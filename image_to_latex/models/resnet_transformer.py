import math
from typing import Any, Dict, List, Optional

import torch
import torch.nn as nn
import torchvision.models

from image_to_latex.models import BaseModel, PositionalEncoding1D, PositionalEncoding2D


TF_DIM = 128
TF_FC_DIM = 256
TF_DROPOUT = 0.4
TF_NUM_LAYERS = 2
TF_NHEAD = 4
MAX_OUTPUT_LENGTH = 250
RESNET_NUM_LAYERS = 2


def generate_square_subsequent_mask(size: int) -> torch.Tensor:
    """Generate a triangular (size, size) mask."""
    mask = (torch.triu(torch.ones(size, size)) == 1).transpose(0, 1)
    mask = (
        mask.float()
        .masked_fill(mask == 0, float("-inf"))
        .masked_fill(mask == 1, float(0.0))
    )
    return mask


class ResnetTransformer(BaseModel):
    """Resnet as encoder and transformer as decoder.

    Attributes:
        num_classes: Vocabulary size
        tf_dim: This serves multiple roles:
            - the output dimension of the encoder,
            - the input dimension of the decoder,
            - the dimension of feedforward networks in the transformer,
            - the dimension of label embeddings, and
            - the dimension of positional encoding.
        max_output_length: Maximum output length during inference.
        resnet: ResNet model. Pretrained weights are not used because the input
            domain is quite different here.
        encoder_projection: A convoluational layer with kernerl size of 1. It
            aims to reduce the number of channels.
        enc_pos_encoder: 2D positional encoding for the encoder.
        embedding: Embedding layer for the targets.
        y_mask: Mask to prevent attention to read tokens in future positions.
        dec_pos_encoder: 1D positional encoding for the decoder.
        transformer_decoder: Transformer decoder.
        fc: Fully connected layer. The output size must be num_classes.
    """

    def __init__(self, id2token: List[str], config: Dict[str, Any] = None) -> None:
        super().__init__(id2token, config)

        self.tf_dim = self.config.get("tf_dim", TF_DIM)
        tf_fc_dim = self.config.get("tf_fc_dim", TF_FC_DIM)
        tf_nhead = self.config.get("tf_nhead", TF_NHEAD)
        tf_dropout = self.config.get("tf_dropout", TF_DROPOUT)
        tf_num_layers = self.config.get("tf_num_layers", TF_NUM_LAYERS)
        self.max_output_length = self.config.get("max_output_length", MAX_OUTPUT_LENGTH)

        # Encoder
        resnet = torchvision.models.resnet18(pretrained=False)
        resnet_num_layers = self.config.get("resnet_num_layers", RESNET_NUM_LAYERS)
        resnet_layers = [resnet.conv1, resnet.bn1, resnet.relu, resnet.maxpool]
        for i in range(1, resnet_num_layers + 1):
            resnet_layers.append(getattr(resnet, f"layer{i}"))
        self.resnet = nn.Sequential(*resnet_layers)
        # Get the output dimension of the last block of the last layer
        # layer1: 64, layer2: 128, layer3: 256, layer4: 512
        resnet_dim = resnet_layers[-1][-1].conv2.out_channels
        self.encoder_projection = nn.Conv2d(resnet_dim, self.tf_dim, 1)
        self.enc_pos_encoder = PositionalEncoding2D(self.tf_dim)

        # Decoder
        self.embedding = nn.Embedding(self.num_classes, self.tf_dim)
        self.y_mask = generate_square_subsequent_mask(self.max_output_length)
        self.dec_pos_encoder = PositionalEncoding1D(
            d_model=self.tf_dim, max_len=self.max_output_length
        )
        self.transformer_decoder = nn.TransformerDecoder(
            decoder_layer=nn.TransformerDecoderLayer(
                self.tf_dim, tf_nhead, tf_fc_dim, tf_dropout
            ),
            num_layers=tf_num_layers,
        )
        self.fc = nn.Linear(self.tf_dim, self.num_classes)

        # It is empirically important to initialize weights properly
        self.init_weights()

    def init_weights(self) -> None:
        """Initialize weights."""
        initrange = 0.1
        self.embedding.weight.data.uniform_(-initrange, initrange)
        self.fc.bias.data.zero_()
        self.fc.weight.data.uniform_(-initrange, initrange)

        nn.init.kaiming_normal_(
            self.encoder_projection.weight.data,
            a=0,
            mode="fan_out",
            nonlinearity="relu",
        )
        if self.encoder_projection.bias is not None:
            _, fan_out = nn.init._calculate_fan_in_and_fan_out(
                self.encoder_projection.weight.data
            )
            bound = 1 / math.sqrt(fan_out)
            nn.init.normal_(self.encoder_projection.bias, -bound, bound)

    def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: (B, _E, _H, _W)
            y: (B, Sy) with elements in (0, num_classes - 1)

        Returns:
            (B, num_classes, Sy) logits
        """
        encoded_x = self.encode(x)  # (Sx, B, E)
        output = self.decode(y, encoded_x)  # (Sy, B, num_classes)
        return output.permute(1, 2, 0)  # (B, num_classes, Sy)

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        """Encode inputs.

        Args:
            x: (B, _E, _H, _W)

        Returns:
            (Sx, B, E)
        """
        _E = x.shape[1]
        # Resnet expects 3 channels but training images are in gray scale
        if _E == 1:
            x = x.repeat(1, 3, 1, 1)
        x = self.resnet(x)  # (B, RESNET_DIM, H, W); H = _H // 32, W = _W // 32
        x = self.encoder_projection(x)  # (B, E, H, W)
        x = self.enc_pos_encoder(x)  # (B, E, H, W)
        x = torch.flatten(x, start_dim=2)  # (B, E, H * W)
        x = x.permute(2, 0, 1)  # (Sx, B, E); Sx = H * W
        return x

    def decode(self, y: torch.Tensor, encoded_x: torch.Tensor) -> torch.Tensor:
        """Decode encoded inputs with teacher-forcing.

        Args:
            encoded_x: (Sx, B, E)
            y: (B, Sy) with elements in (0, num_classes - 1)

        Returns:
            (Sy, B, num_classes) logits
        """
        y = y.permute(1, 0)  # (Sy, B)
        y = self.embedding(y) * math.sqrt(self.tf_dim)  # (Sy, B, E)
        y = self.dec_pos_encoder(y)  # (Sy, B, E)
        Sy = y.shape[0]
        y_mask = self.y_mask[:Sy, :Sy].type_as(encoded_x)  # (Sy, Sy)
        output = self.transformer_decoder(y, encoded_x, y_mask)  # (Sy, B, E)
        output = self.fc(output)  # (Sy, B, num_classes)
        return output

    def predict(
        self,
        x: torch.Tensor,
        max_output_length: Optional[int] = None,
    ) -> torch.Tensor:
        """Make predctions at inference time.

        Predict y from x one token at a time. This method is greedy decoding.
        Beam search can be used instead for a potential accuracy boost.

        Args:
            x: (B, H, W) images
            max_output_length: Maximum output length. Can be smaller than the
                one in positional encoding.

        Returns:
            (B, max_output_length) with elements in (0, num_classes - 1).
        """
        if max_output_length is None:
            max_output_length = self.max_output_length
        elif max_output_length > self.max_output_length:
            raise ValueError(
                f"max_output_length is expected to be smaller than {self.max_output_length}"
            )

        B = x.shape[0]
        S = max_output_length

        encoded_x = self.encode(x)  # (Sx, B, E)

        output_tokens = (
            torch.full((B, S), self.padding_index).type_as(x).long()
        )  # (B, S)
        output_tokens[:, 0] = self.start_index
        for Sy in range(1, S):
            y = output_tokens[:, :Sy]  # (B, Sy)
            logits = self.decode(y, encoded_x)  # (Sy, B, C)
            output = torch.argmax(logits, dim=-1)  # (Sy, B)
            output_tokens[:, Sy] = output[-1:]  # Set the last output token

            # Early stopping of prediction loop to speed up prediction
            current_tokens = output_tokens[:, Sy]
            is_ended = current_tokens == self.end_index
            is_padded = current_tokens == self.padding_index
            if (is_ended | is_padded).all():
                break

        # Set all tokens after end token to be padding
        for Sy in range(1, S):
            previous_tokens = output_tokens[:, Sy - 1]
            is_ended = previous_tokens == self.end_index
            is_padded = previous_tokens == self.padding_index
            output_tokens[(is_ended | is_padded), Sy] = self.padding_index

        return output_tokens

    @staticmethod
    def add_to_argparse(parser):
        """Add arguments to a parser."""
        parser.add_argument("--resnet_num_layers", type=int, default=RESNET_NUM_LAYERS)
        parser.add_argument("--tf_dim", type=int, default=TF_DIM)
        parser.add_argument("--tf_fc_dim", type=int, default=TF_FC_DIM)
        parser.add_argument("--tf_dropout", type=float, default=TF_DROPOUT)
        parser.add_argument("--tf_num_layers", type=int, default=TF_NUM_LAYERS)
        parser.add_argument("--tf_nhead", type=int, default=TF_NHEAD)
        parser.add_argument("--max_output_length", type=int, default=MAX_OUTPUT_LENGTH)
        return parser

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

from typing import Optional, Tuple, Type

import torch
from torch import nn

from sam2.modeling.position_encoding import PositionEmbeddingRandom

from sam2.modeling.sam2_utils import LayerNorm2d


class PromptEncoder(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        image_embedding_size: Tuple[int, int],
        input_image_size: Tuple[int, int],
        mask_in_chans: int,
        activation: Type[nn.Module] = nn.GELU,
    ) -> None:
        """
        Encodes prompts for input to SAM's mask decoder.

        Arguments:
          embed_dim (int): The prompts' embedding dimension
          image_embedding_size (tuple(int, int)): The spatial size of the
            image embedding, as (H, W).
          input_image_size (int): The padded size of the image as input
            to the image encoder, as (H, W).
          mask_in_chans (int): The number of hidden channels used for
            encoding input masks.
          activation (nn.Module): The activation to use when encoding
            input masks.
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.input_image_size = input_image_size
        self.image_embedding_size = image_embedding_size
        self.pe_layer = PositionEmbeddingRandom(embed_dim // 2)

        self.num_point_embeddings: int = 4  # pos/neg point + 2 box corners
        point_embeddings = [
            nn.Embedding(1, embed_dim) for i in range(self.num_point_embeddings)
        ]
        self.point_embeddings = nn.ModuleList(point_embeddings)
        self.not_a_point_embed = nn.Embedding(1, embed_dim)

        self.mask_input_size = (
            4 * image_embedding_size[0],
            4 * image_embedding_size[1],
        )
        self.mask_downscaling = nn.Sequential(
            nn.Conv2d(1, mask_in_chans // 4, kernel_size=2, stride=2),
            LayerNorm2d(mask_in_chans // 4),
            activation(),
            nn.Conv2d(mask_in_chans // 4, mask_in_chans, kernel_size=2, stride=2),
            LayerNorm2d(mask_in_chans),
            activation(),
            nn.Conv2d(mask_in_chans, embed_dim, kernel_size=1),
        )
        self.no_mask_embed = nn.Embedding(1, embed_dim)

    def get_dense_pe(self) -> torch.Tensor:
        """
        Returns the positional encoding used to encode point prompts,
        applied to a dense set of points the shape of the image encoding.

        Returns:
          torch.Tensor: Positional encoding with shape
            1x(embed_dim)x(embedding_h)x(embedding_w)
        """
        return self.pe_layer(self.image_embedding_size).unsqueeze(0)

    def _embed_points(
        self,
        points: torch.Tensor,
        labels: torch.Tensor,
        pad: bool,
    ) -> torch.Tensor:
        """Embeds point prompts."""
        points = points + 0.5  # Shift to center of pixel
        if pad:
            padding_point = torch.zeros((points.shape[0], 1, 2), device=points.device)
            padding_label = -torch.ones((labels.shape[0], 1), device=labels.device)
            points = torch.cat([points, padding_point], dim=1)
            labels = torch.cat([labels, padding_label], dim=1)
        point_embedding = self.pe_layer.forward_with_coords(
            points, self.input_image_size
        )

        point_embedding = torch.where(
            (labels == -1).unsqueeze(-1),
            torch.zeros_like(point_embedding) + self.not_a_point_embed.weight,
            point_embedding,
        )
        point_embedding = torch.where(
            (labels == 0).unsqueeze(-1),
            point_embedding + self.point_embeddings[0].weight,
            point_embedding,
        )
        point_embedding = torch.where(
            (labels == 1).unsqueeze(-1),
            point_embedding + self.point_embeddings[1].weight,
            point_embedding,
        )
        point_embedding = torch.where(
            (labels == 2).unsqueeze(-1),
            point_embedding + self.point_embeddings[2].weight,
            point_embedding,
        )
        point_embedding = torch.where(
            (labels == 3).unsqueeze(-1),
            point_embedding + self.point_embeddings[3].weight,
            point_embedding,
        )
        return point_embedding

    def _embed_boxes(self, boxes: torch.Tensor) -> torch.Tensor:
        """Embeds box prompts."""
        boxes = boxes + 0.5  # Shift to center of pixel
        coords = boxes.reshape(-1, 2, 2)
        corner_embedding = self.pe_layer.forward_with_coords(
            coords, self.input_image_size
        )
        corner_embedding[:, 0, :] += self.point_embeddings[2].weight
        corner_embedding[:, 1, :] += self.point_embeddings[3].weight
        return corner_embedding

    def _embed_masks(self, masks: torch.Tensor) -> torch.Tensor:
        """Embeds mask inputs."""
        mask_embedding = self.mask_downscaling(masks)
        return mask_embedding

    def _get_batch_size(
        self,
        points: Optional[Tuple[torch.Tensor, torch.Tensor]],
        boxes: Optional[torch.Tensor],
        masks: Optional[torch.Tensor],
    ) -> int:
        """
        Gets the batch size of the output given the batch size of the input prompts.
        """
        if points is not None:
            return points[0].shape[0]
        elif boxes is not None:
            return boxes.shape[0]
        elif masks is not None:
            return masks.shape[0]
        else:
            return 1

    def _get_device(self) -> torch.device:
        return self.point_embeddings[0].weight.device

    def forward(
        self,
        points: Optional[Tuple[torch.Tensor, torch.Tensor]],
        boxes: Optional[torch.Tensor],
        masks: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Embeds different types of prompts, returning both sparse and dense
        embeddings.

        Arguments:
          points (tuple(torch.Tensor, torch.Tensor) or none): point coordinates
            and labels to embed.
          boxes (torch.Tensor or none): boxes to embed
          masks (torch.Tensor or none): masks to embed

        Returns:
          torch.Tensor: sparse embeddings for the points and boxes, with shape
            BxNx(embed_dim), where N is determined by the number of input points
            and boxes.
          torch.Tensor: dense embeddings for the masks, in the shape
            Bx(embed_dim)x(embed_H)x(embed_W)
        """
        bs = self._get_batch_size(points, boxes, masks)
        sparse_embeddings = torch.empty(
            (bs, 0, self.embed_dim), device=self._get_device()
        )
        if points is not None:
            coords, labels = points
            point_embeddings = self._embed_points(coords, labels, pad=(boxes is None))
            sparse_embeddings = torch.cat([sparse_embeddings, point_embeddings], dim=1)
        if boxes is not None:
            box_embeddings = self._embed_boxes(boxes)
            sparse_embeddings = torch.cat([sparse_embeddings, box_embeddings], dim=1)

        if masks is not None:
            dense_embeddings = self._embed_masks(masks)
        else:
            dense_embeddings = self.no_mask_embed.weight.reshape(1, -1, 1, 1).expand(
                bs, -1, self.image_embedding_size[0], self.image_embedding_size[1]
            )

        return sparse_embeddings, dense_embeddings

class CustomPromptEncoder(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        image_embedding_size: Tuple[int, int],
        input_image_size: Tuple[int, int],
        mask_in_chans: int,
        activation: Type[nn.Module] = nn.GELU,
        num_tokens: int = 4,
    ) -> None:
        """
        Drop-in replacement for the original PromptEncoder that:
        1) Has the same signature
        2) Ignores real prompts in forward()
        3) Returns purely learned embeddings
        4) Provides a get_dense_pe() for the mask decoder
        """

        super().__init__()
        self.embed_dim = embed_dim
        self.image_embedding_size = image_embedding_size
        self.input_image_size = input_image_size
        self.mask_in_chans = mask_in_chans
        self.activation = activation
        self.num_tokens = num_tokens

        # 1) Trainable sparse tokens (shape: num_tokens x embed_dim)
        self.sparse_tokens = nn.Parameter(torch.randn(num_tokens, embed_dim))

        # 2) Trainable dense embedding used as "mask embedding"
        H, W = self.image_embedding_size
        self.dense_emb = nn.Parameter(torch.randn(embed_dim, H, W))

        # 3) Optionally, a separate trainable "dense_pe" for the image positional encoding
        self.learned_pe = nn.Parameter(torch.randn(embed_dim, H, W))

    def forward(
        self,
        points: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        boxes: Optional[torch.Tensor] = None,
        masks: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Ignores the input prompts and returns purely learned embeddings.

        Returns:
          sparse_embeddings: (B, num_tokens, embed_dim)
          dense_embeddings:  (B, embed_dim, H, W)
        """
        # Derive batch size from the first non-None input, otherwise default to 1
        if points is not None:
            bs = points[0].shape[0]  # (coords, labels) => points[0] is BxPx2
        elif boxes is not None:
            bs = boxes.shape[0]
        elif masks is not None:
            bs = masks.shape[0]
        else:
            bs = 1

        # 1) Sparse: Expand tokens across the batch dimension
        sparse_embeddings = self.sparse_tokens.unsqueeze(0).expand(bs, -1, -1)

        # 2) Dense: Expand learned embedding across batch dimension
        dense_embeddings = self.dense_emb.unsqueeze(0).expand(bs, -1, -1, -1)

        return sparse_embeddings, dense_embeddings

    def get_dense_pe(self) -> torch.Tensor:
        """
        Returns a trainable image positional encoding of shape
        1 x embed_dim x H x W.
        """
        return self.learned_pe.unsqueeze(0)


class CustomPromptEncoderLarger(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        image_embedding_size: Tuple[int, int],
        input_image_size: Tuple[int, int],
        mask_in_chans: int,
        activation: Type[nn.Module] = nn.GELU,
        num_tokens: int = 4,
        mlp_hidden_factor: int = 4,  # how large to make the MLP hidden dim
        num_conv_layers: int = 2,    # how many conv layers to apply on dense_emb
    ) -> None:
        """
        Drop-in replacement for the original PromptEncoder that:
        1) Has the same signature
        2) Ignores real prompts in forward()
        3) Returns purely learned embeddings
        4) Provides a get_dense_pe() for the mask decoder
        5) Uses small MLP / Conv blocks to increase trainable parameter capacity

        Args:
            embed_dim (int): final embedding dimension.
            image_embedding_size (Tuple[int, int]): (H, W) for the output feature map.
            input_image_size (Tuple[int, int]): (ignored, for interface compat).
            mask_in_chans (int): (ignored, for interface compat).
            activation (Type[nn.Module]): activation class (e.g. nn.GELU).
            num_tokens (int): how many sparse prompt tokens to learn.
            mlp_hidden_factor (int): hidden dimension multiplier for the MLP on sparse tokens.
            num_conv_layers (int): how many conv layers to apply on the dense embedding.
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.image_embedding_size = image_embedding_size
        self.input_image_size = input_image_size
        self.mask_in_chans = mask_in_chans
        self.activation = activation
        self.num_tokens = num_tokens

        # ---------------------------------------------------------
        # 1) Trainable base parameters
        # ---------------------------------------------------------
        # a) Sparse tokens (num_tokens x embed_dim)
        self.sparse_tokens = nn.Parameter(torch.randn(num_tokens, embed_dim))

        # b) Dense embedding for the "mask" branch (embed_dim x H x W)
        H, W = self.image_embedding_size
        self.dense_emb = nn.Parameter(torch.randn(embed_dim, H, W))

        # c) Learned positional encoding (embed_dim x H x W)
        self.learned_pe = nn.Parameter(torch.randn(embed_dim, H, W))

        # ---------------------------------------------------------
        # 2) Additional MLP to transform sparse tokens
        # ---------------------------------------------------------
        # We'll transform each token from size (embed_dim) -> (embed_dim),
        # using a hidden layer of size embed_dim * mlp_hidden_factor.
        hidden_dim = embed_dim * mlp_hidden_factor
        self.sparse_mlp = nn.Sequential(
            nn.Linear(embed_dim, hidden_dim),
            self.activation(),
            nn.Linear(hidden_dim, embed_dim),
        )

        # ---------------------------------------------------------
        # 3) Additional conv stack to transform the dense_emb
        # ---------------------------------------------------------
        # We apply a small series of conv layers on the shape (embed_dim, H, W).
        # Using groups=1, kernel_size=3, etc. as an example. 
        # You can insert BN/LayerNorm if you like.
        conv_layers = []
        in_channels = embed_dim
        for _ in range(num_conv_layers):
            conv_layers.append(nn.Conv2d(in_channels, embed_dim, kernel_size=1, padding=0, stride=1))
            conv_layers.append(self.activation())
            in_channels = embed_dim
        self.dense_conv = nn.Sequential(*conv_layers)

    def forward(
        self,
        points: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        boxes: Optional[torch.Tensor] = None,
        masks: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Ignoring input prompts, returning:
          sparse_embeddings: (B, num_tokens, embed_dim)
          dense_embeddings:  (B, embed_dim, H, W)
        """
        # ---------------------------------------------------------
        # 1) Figure out batch size
        # ---------------------------------------------------------
        if points is not None:
            bs = points[0].shape[0]
        elif boxes is not None:
            bs = boxes.shape[0]
        elif masks is not None:
            bs = masks.shape[0]
        else:
            bs = 1

        # ---------------------------------------------------------
        # 2) Transform the sparse tokens
        # ---------------------------------------------------------
        # a) shape: (num_tokens, embed_dim)
        tokens = self.sparse_tokens
        # b) apply MLP, still shape (num_tokens, embed_dim)
        tokens = self.sparse_mlp(tokens)
        # c) expand across batch dimension => (B, num_tokens, embed_dim)
        sparse_embeddings = tokens.unsqueeze(0).expand(bs, -1, -1)

        # ---------------------------------------------------------
        # 3) Transform the dense embedding
        # ---------------------------------------------------------
        # a) shape: (embed_dim, H, W)
        dense = self.dense_emb
        # b) feed it through the conv stack => (embed_dim, H, W)
        # note: conv expects (B, C, H, W), so we temporarily unsqueeze
        dense = dense.unsqueeze(0)  # shape (1, embed_dim, H, W)
        dense = self.dense_conv(dense)  # shape (1, embed_dim, H, W)
        # we now expand across batch dimension => (B, embed_dim, H, W)
        dense_embeddings = dense.expand(bs, -1, -1, -1)

        return sparse_embeddings, dense_embeddings

    def get_dense_pe(self) -> torch.Tensor:
        """
        Return a trainable image positional encoding of shape (1, embed_dim, H, W).
        """
        return self.learned_pe.unsqueeze(0)

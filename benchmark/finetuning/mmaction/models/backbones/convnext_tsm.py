import torch
import torch.nn as nn
from torch.nn.modules.utils import _ntuple

from ..builder import BACKBONES
from .convnext import ConvNeXt


class TemporalShift(nn.Module):
    """Temporal shift module.

    This module is proposed in
    `TSM: Temporal Shift Module for Efficient Video Understanding
    <https://arxiv.org/abs/1811.08383>`_

    Args:
        net (nn.module): Module to make temporal shift.
        num_segments (int): Number of frame segments. Default: 3.
        shift_div (int): Number of divisions for shift. Default: 8.
    """

    def __init__(self, net, num_segments=3, shift_div=8):
        super().__init__()
        self.net = net
        self.num_segments = num_segments
        self.shift_div = shift_div

    def forward(self, x):
        """Defines the computation performed at every call.

        Args:
            x (torch.Tensor): The input data.

        Returns:
            torch.Tensor: The output of the module.
        """
        x = self.shift(x, self.num_segments, shift_div=self.shift_div)
        return self.net(x)

    @staticmethod
    def shift(x, num_segments, shift_div=3):
        """Perform temporal shift operation on the feature.

        Args:
            x (torch.Tensor): The input feature to be shifted.
            num_segments (int): Number of frame segments.
            shift_div (int): Number of divisions for shift. Default: 3.

        Returns:
            torch.Tensor: The shifted feature.
        """
        # [N, C, H, W]
        n, c, h, w = x.size()

        # [N // num_segments, num_segments, C, H*W]
        # can't use 5 dimensional array on PPL2D backend for caffe
        x = x.view(-1, num_segments, c, h * w)

        # get shift fold
        fold = c // shift_div

        # split c channel into three parts:
        # left_split, mid_split, right_split
        left_split = x[:, :, :fold, :]
        mid_split = x[:, :, fold:2 * fold, :]
        right_split = x[:, :, 2 * fold:, :]

        # can't use torch.zeros(*A.shape) or torch.zeros_like(A)
        # because array on caffe inference must be got by computing

        # shift left on num_segments channel in `left_split`
        zeros = left_split - left_split
        blank = zeros[:, :1, :, :]
        left_split = left_split[:, 1:, :, :]
        left_split = torch.cat((left_split, blank), 1)

        # shift right on num_segments channel in `mid_split`
        zeros = mid_split - mid_split
        blank = zeros[:, :1, :, :]
        mid_split = mid_split[:, :-1, :, :]
        mid_split = torch.cat((blank, mid_split), 1)

        # right_split: no shift

        # concatenate
        out = torch.cat((left_split, mid_split, right_split), 2)

        # [N, C, H, W]
        # restore the original dimension
        return out.view(n, c, h, w)


@BACKBONES.register_module()
class ConvNeXtTSM(ConvNeXt):
    """ConvNeXt backbone for TSM.

    Args:
        num_segments (int): Number of frame segments. Default: 8.
        is_shift (bool): Whether to make temporal shift in reset layers.
            Default: True.
        shift_div (int): Number of div for shift. Default: 8.
        shift_place (str): Places in convnext layers for shift, which is chosen
            from ['block', 'block_convnext'].
            If set to 'block', it will apply temporal shift to all child blocks
            in each convnext layer.
            If set to 'block_convnext', it will apply temporal shift to each `dwconv`
            layer of all child blocks in each convnext layer.
            Default: 'block_convnext'.
        temporal_pool (bool): Whether to add temporal pooling. Default: False.
        **kwargs (keyword arguments, optional): Arguments for ResNet.
    """

    def __init__(self,
                 name,
                 num_segments=8,
                 is_shift=True,
                 shift_div=8,
                 shift_place='block_convnext',
                 temporal_pool=False,
                 **kwargs):
        super().__init__(name, **kwargs)
        self.num_segments = num_segments
        self.is_shift = is_shift
        self.shift_div = shift_div
        self.shift_place = shift_place
        self.temporal_pool = temporal_pool


    def make_temporal_shift(self):
        """Make temporal shift for some layers."""
        if self.temporal_pool:
            num_segment_list = [
                self.num_segments, self.num_segments // 2,
                self.num_segments // 2, self.num_segments // 2
            ]
        else:
            num_segment_list = [self.num_segments] * 4
        if num_segment_list[-1] <= 0:
            raise ValueError('num_segment_list[-1] must be positive')

        if self.shift_place == 'block':

            def make_block_temporal(stage, num_segments):
                """Make temporal shift on some blocks.

                Args:
                    stage (nn.Module): Model layers to be shifted.
                    num_segments (int): Number of frame segments.

                Returns:
                    nn.Module: The shifted blocks.
                """
                blocks = list(stage.children())
                for i, b in enumerate(blocks):
                    blocks[i] = TemporalShift(
                        b, num_segments=num_segments, shift_div=self.shift_div)
                return nn.Sequential(*blocks)

            self.stages[0] = make_block_temporal(self.stages[0], num_segment_list[0])
            self.stages[1] = make_block_temporal(self.stages[1], num_segment_list[1])
            self.stages[2] = make_block_temporal(self.stages[2], num_segment_list[2])
            self.stages[3] = make_block_temporal(self.stages[3], num_segment_list[3])

        elif 'block_convnext' in self.shift_place:
            n_round = 1
            if len(list(self.stages[3].children())) >= 23:
                n_round = 2

            def make_block_temporal(stage, num_segments):
                """Make temporal shift on some blocks.

                Args:
                    stage (nn.Module): Model layers to be shifted.
                    num_segments (int): Number of frame segments.

                Returns:
                    nn.Module: The shifted blocks.
                """
                blocks = list(stage.children())
                for i, b in enumerate(blocks):
                    if i % n_round == 0:
                        blocks[i].dwconv = TemporalShift(
                            b.dwconv,
                            num_segments=num_segments,
                            shift_div=self.shift_div)
                return nn.Sequential(*blocks)

            self.stages[0] = make_block_temporal(self.stages[0], num_segment_list[0])
            self.stages[1] = make_block_temporal(self.stages[1], num_segment_list[1])
            self.stages[2] = make_block_temporal(self.stages[2], num_segment_list[2])
            self.stages[3] = make_block_temporal(self.stages[3], num_segment_list[3])

        else:
            raise NotImplementedError

    def make_temporal_pool(self):
        """Make temporal pooling between layer1 and layer2, using a 3D max
        pooling layer."""

        class TemporalPool(nn.Module):
            """Temporal pool module.

            Wrap layer2 in ResNet50 with a 3D max pooling layer.

            Args:
                net (nn.Module): Module to make temporal pool.
                num_segments (int): Number of frame segments.
            """

            def __init__(self, net, num_segments):
                super().__init__()
                self.net = net
                self.num_segments = num_segments
                self.max_pool3d = nn.MaxPool3d(
                    kernel_size=(3, 1, 1), stride=(2, 1, 1), padding=(1, 0, 0))

            def forward(self, x):
                # [N, C, H, W]
                n, c, h, w = x.size()
                # [N // num_segments, C, num_segments, H, W]
                x = x.view(n // self.num_segments, self.num_segments, c, h,
                           w).transpose(1, 2)
                # [N // num_segmnets, C, num_segments // 2, H, W]
                x = self.max_pool3d(x)
                # [N // 2, C, H, W]
                x = x.transpose(1, 2).contiguous().view(n // 2, c, h, w)
                return self.net(x)

        self.layer2 = TemporalPool(self.layer2, self.num_segments)


    def init_weights(self):
        """Initiate the parameters either from existing checkpoint or from
        scratch."""
        super().init_weights()
        if self.is_shift:
            self.make_temporal_shift()
        if self.temporal_pool:
            self.make_temporal_pool()

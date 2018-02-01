'''
Merging-and-Evolution Network
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from collections import OrderedDict
from .common import channel_shuffle

__all__ = [
    'menet',
]


def depthwise_conv(c, stride):
    return nn.Conv2d(c, c, 3, stride=stride, padding=1, groups=c, bias=False)


def group_conv(in_c, out_c, groups):
    return nn.Conv2d(in_c, out_c, 1, groups=groups, bias=False)


def conv1x1(in_c, out_c):
    return nn.Conv2d(in_c, out_c, 1, bias=False)


def conv3x3(in_c, out_c, stride):
    return nn.Conv2d(in_c, out_c, 3, stride=stride, padding=1, bias=False)


class _MEModule(nn.Module):
    def __init__(self, in_c, out_c, side_c, downsample, groups, ignore_group):
        super(_MEModule, self).__init__()
        bott = out_c // 4
        self.downsample = downsample
        self.groups = groups
        if downsample:
            out_c -= in_c
            # residual branch
            if ignore_group:
                self.compress = group_conv(in_c, bott, 1)
            else:
                self.compress = group_conv(in_c, bott, groups)
            self.bn_compress = nn.BatchNorm2d(bott)
            self.depthwise = depthwise_conv(bott, 2)
            self.bn_depthwise = nn.BatchNorm2d(bott)
            self.expand = group_conv(bott, out_c, groups)
            self.bn_expand = nn.BatchNorm2d(out_c)
            self.pool = nn.AvgPool2d(3, stride=2, padding=1)
            # fusion branch
            self.s_merge = conv1x1(bott, side_c)
            self.s_bn_merge = nn.BatchNorm2d(side_c)
            self.s_conv = conv3x3(side_c, side_c, 2)
            self.s_bn_conv = nn.BatchNorm2d(side_c)
            self.s_evolve = conv1x1(side_c, bott)
            self.s_bn_evolve = nn.BatchNorm2d(bott)
        else:
            # residual branch
            self.compress = group_conv(in_c, bott, groups)
            self.bn_compress = nn.BatchNorm2d(bott)
            self.depthwise = depthwise_conv(bott, 1)
            self.bn_depthwise = nn.BatchNorm2d(bott)
            self.expand = group_conv(bott, out_c, groups)
            self.bn_expand = nn.BatchNorm2d(out_c)
            # fusion branch
            self.s_merge = conv1x1(bott, side_c)
            self.s_bn_merge = nn.BatchNorm2d(side_c)
            self.s_conv = conv3x3(side_c, side_c, 1)
            self.s_bn_conv = nn.BatchNorm2d(side_c)
            self.s_evolve = conv1x1(side_c, bott)
            self.s_bn_evolve = nn.BatchNorm2d(bott)

    def forward(self, x):
        identity = x
        # pointwise group convolution 1
        x = self.compress(x)
        x = self.bn_compress(x)
        x = F.relu(x, inplace=True)
        x = channel_shuffle(x, self.groups)
        # merging
        y = self.s_merge(x)
        y = self.s_bn_merge(y)
        y = F.relu(y, inplace=True)
        # depthwise convolution (bottleneck)
        x = self.depthwise(x)
        x = self.bn_depthwise(x)
        # evolution
        y = self.s_conv(y)
        y = self.s_bn_conv(y)
        y = F.relu(y, inplace=True)
        y = self.s_evolve(y)
        y = self.s_bn_evolve(y)
        y = F.sigmoid(y)
        x *= y
        # pointwise group convolution 2
        x = self.expand(x)
        x = self.bn_expand(x)
        # identity branch
        if self.downsample:
            identity = self.pool(identity)
            x = torch.cat((x, identity), dim=1)
        else:
            x += identity
        x = F.relu(x, inplace=True)
        return x


class _InitBlock(nn.Module):
    def __init__(self, init_c):
        super(_InitBlock, self).__init__()
        self.conv = conv3x3(3, init_c, 2)
        self.bn = nn.BatchNorm2d(init_c)
        self.pool = nn.MaxPool2d(3, stride=2, padding=1)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = F.relu(x, inplace=True)
        x = self.pool(x)
        return x


class MENet(nn.Module):
    def __init__(self, block_channels, block_layers, init_c, side_channels, groups):
        super(MENet, self).__init__()
        self.features = nn.Sequential(OrderedDict([
            ('init', _InitBlock(init_c)),
        ]))
        in_c = init_c
        for i, (out_c, num_layers, side_c) in enumerate(zip(block_channels, block_layers, side_channels)):
            self.features.add_module(
                'stage_{}_{}'.format(i + 1, 1),
                _MEModule(in_c, out_c, side_c, True, groups, (i == 0))
            )
            for _ in range(num_layers):
                self.features.add_module(
                    'stage_{}_{}'.format(i + 1, _ + 2),
                    _MEModule(out_c, out_c, side_c, False, groups, False)
                )
            in_c = out_c
        self.pool = nn.AvgPool2d(7)
        self.classifier = nn.Linear(in_c, 1000)

    def forward(self, x):
        x = self.features(x)
        x = self.pool(x)
        x = x.view(x.size(0), -1)
        x = self.classifier(x)
        return x


def menet(model_config):
    block_channels = model_config['block_channels']
    block_layers = model_config['block_layers']
    init_c = model_config['init_c']
    side_channels = model_config['side_channels']
    groups = model_config['groups']
    return MENet(block_channels, block_layers, init_c, side_channels, groups)

"""Shioaji adapter submodules.

Import concrete classes from their dedicated modules, e.g.:
`from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade`.
"""

from hft_platform.feed_adapter.shioaji.data_gateway import DataGateway

__all__: list[str] = ["DataGateway"]

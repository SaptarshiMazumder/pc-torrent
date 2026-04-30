"""Download: blend-file fetching with HTTP-Range resume on connection drops.

RangeResumer    -- low-level transport: GET stream + Range-resume retry
BlendDownloader -- orchestration: download + (optional) zip extract
"""

from .range_resumer import RangeResumer
from .blend_downloader import BlendDownloader

__all__ = ["RangeResumer", "BlendDownloader"]

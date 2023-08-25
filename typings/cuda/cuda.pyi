from enum import Enum
class CUresult(Enum):
    CUDA_SUCCESS = 0

class _CUMixin:
    def getPtr(self) -> int: ...

class CUdevice(_CUMixin): ...

def cuDeviceGetCount() -> tuple[CUresult, int]: ...
def cuDeviceGet(dev: int) -> tuple[CUresult, CUdevice]: ...
def cuInit(flags: int) -> tuple[CUresult]: ...

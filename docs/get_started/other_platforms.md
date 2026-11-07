# Platform Installation

The default [installation](./installation.md) targets NVIDIA CUDA GPUs. When
you are on a different accelerator or need a platform-specific wheel stack, use
one of the guides below. Each page pins the matching runtime, PyTorch index, and
build flags so the CUDA wheel does not clobber a non-CUDA environment.

Pick the guide that matches your hardware:

```{toctree}
:maxdepth: 1
:titlesonly:

installation_npu
installation_xpu
installation_cpu
installation_musa
```

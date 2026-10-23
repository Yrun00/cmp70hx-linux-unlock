# Source provenance and licenses

This is a mixed-license source distribution. The Python installer, configuration
tools, tests and documentation use the root [MIT license](LICENSE). The modified
driver contains GPL-2.0-only code: distribute the combined driver under GPL-2.0-only,
retain the MIT notices for its MIT portions, and provide its corresponding source
and build/configuration material when distributing a compiled module.

## Direct sources

| Source | Included/adapted material | License |
|---|---|---|
| [NVIDIA OPEN 610.43.03](https://github.com/NVIDIA/open-gpu-kernel-modules/tree/452cec62d827034798072827d3866d1881662b77) | Base kernel sources; original notices remain in each file | NVIDIA MIT notices / upstream dual licensing |
| [pearlfortune/cmpunlocker v0.1.28](https://github.com/pearlfortune/cmpunlocker/releases/tag/v0.1.28) | Stockflow rejoin14 multi-GPU state and rejoin15 serialized startup patches | [pearlfortune MIT notice](LICENSES/pearlfortune-MIT.txt) |
| [jdowning100 rejoin16](https://github.com/jdowning100/cmpunlocker/blob/2cdc63cf1ab80d8128cf9e77216e3f3c132b212f/90hx/0016-6104303-cmp90hx-stockflow-rejoin16-pcie-jtag-plm.patch) | Protected-write continuation and PCIe policy adapted to GA104 | [GPL-2.0-only](LICENSES/GPL-2.0-only.txt) |

The saved rejoin16 input matches the linked commit byte for byte (SHA256
`2fd97d6b04938017e85181b4900f78daf4e2c560a4ccd86e917aa7ddff05559e`).
Rejoin14/15 came from `cmpunlocker-v0.1.28-linux-x64-90hx-stockflow.tar.gz`,
whose upstream release digest is
`19c67cd0cb2df5ee10ea28c97ada756ada309553f2500129f973c3adf84f99c3`.
`PROVENANCE.json` records patch/template hashes and file-level origins.

## Overlay map

Paths below are relative to `overlay/`. Adaptations were modified for this
release on 2026-10-22; these are modified sources, not pristine upstream files.

| File | Origin and modifications | Distribution license |
|---|---|---|
| `kernel-open/nvidia/nv.c` | NVIDIA base; rejoin14 retry, rejoin15 serialization, rejoin16 PCIe policy; GA104 device guards, staged activation, link-width checks and GFX selection | GPL-2.0-only combined file; retain NVIDIA/pearlfortune notices |
| `src/nvidia/src/kernel/gpu/gsp/kernel_gsp.c` | NVIDIA base; rejoin14 per-GPU signature state and stock restoration; rejoin16 protected-write continuation; GA104 selectors and bounds checking | GPL-2.0-only combined file; retain NVIDIA/pearlfortune notices |
| `src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_booter_tu102.c` | NVIDIA base; rejoin14 Booter lifecycle and rejoin16 PLM handoff; GA104 gates | GPL-2.0-only combined file; retain NVIDIA/pearlfortune notices |
| `src/nvidia/src/kernel/gpu/gsp/arch/ampere/kernel_gsp_falcon_ga102.c` | NVIDIA base; pearlfortune rejoin14 scrub/retry path and GA104 target guard | MIT notices retained |
| `src/nvidia/src/kernel/gpu/gsp/arch/turing/kernel_gsp_frts_tu102.c` | NVIDIA base; pearlfortune rejoin14 FRTS handling and GA104 target guard | MIT notices retained |
| `src/nvidia/inc/kernel/gpu/gsp/cmp70_port.h` | Local GA104 configuration/address adapter | MIT |

[Wh1stle05/cmp90hx](https://github.com/Wh1stle05/cmp90hx/tree/477c712e4d5a9c6b296c936d3d1b713bcdbfc1d9)
was an additional register/mechanism reference. The direct patch inputs above
are the source of the imported rejoin implementation.

NVPermissive distribution `380bdf3` was obtained from the third-party
[GraphicsUnlock directory](https://alist.homelabproject.cc/foxipan/vGPU/CMP_90HX/GraphicsUnlock),
as [nvpermissive-dist-380bdf3.tar.gz](https://alist.homelabproject.cc/d/foxipan/vGPU/CMP_90HX/GraphicsUnlock/nvpermissive-dist-380bdf3.tar.gz).
The retained archive's SHA256 is
`d5e89e77e121e1295cb39d331e0d511275b6edc1d7236376e0fd13ac6f0c79c0`.
It was consulted for XP/XVE register definitions and masked PCIe policy operations.
Its README names [GreenDamTan/NVPermissive](https://github.com/GreenDamTan/NVPermissive),
but that GitHub address was unavailable during the research (the recorded API
request returned 404 on 2026-10-15). The archive is the actual source examined;
access to the complete repository was not established.

The archive contains partial sources and a precompiled core. No NVPermissive
object code is included here; the local GA104 PCIe implementation is in `nv.c`
and its generated configuration guards.

The package includes complete modified overlay files, source generation/build
scripts and the exact NVIDIA source-archive hash. The wizard downloads the vendor
source separately. For a generated module, retain its `host.patch`, configuration,
build metadata and the matching upstream source when sharing it.

No proprietary NVIDIA userspace binaries, firmware, installer packages or miners
are redistributed here. NVIDIA and CMP names identify the supported hardware;
this project is not affiliated with NVIDIA.

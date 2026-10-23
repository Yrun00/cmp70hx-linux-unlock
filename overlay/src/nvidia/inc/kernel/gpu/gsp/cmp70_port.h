/* SPDX-License-Identifier: MIT
 * Experimental GA104 adaptation of pearlfortune/jdowning100 rejoin14-16.
 * Targets must be generated from the reviewed host configuration.
 * Mode 0/1: stock path / read-only diagnostic. 2: existing V67 compute path.
 * Mode 3: one additional allowlisted PLM write. No PCIe policy writes here.
 */
#ifndef CMP70_PORT_H
#define CMP70_PORT_H
#error CMP70HX_TEMPLATE_REQUIRES_GENERATION
#include "gpu/gpu.h"
extern NvU32 osCmp70PortMode(void);
static NV_INLINE NvU32 cmp70PortWriteAddress(void);
extern NvU32 osCmp70PortWriteIndex(void);
static NV_INLINE NvBool cmp70PortAddress(OBJGPU *pGpu)
{
    return NV_FALSE; /* Replaced by the builder. */
}
static NV_INLINE NvBool cmp70PortEnabled(OBJGPU *pGpu)
{
    NvU32 mode = osCmp70PortMode();
    return (mode == 2 || mode == 3 || (mode == 4 && cmp70PortWriteAddress())) && cmp70PortAddress(pGpu);
}
static NV_INLINE NvU32 cmp70PortWriteAddress(void)
{
    /* Explicit narrow allowlist from the upstream two-mask lifecycle. */
    switch (osCmp70PortWriteIndex()) {
        case 1: return 0x00088fe8U;
        case 2: return 0x00823800U;
        case 3: return 0x00823b04U; /* Default-off graphics experiment gate. */
        default: return 0;
    }
}
static NV_INLINE NvU32 cmp70PortPrimaryAddress(void)
{
    return osCmp70PortMode() == 4 ? cmp70PortWriteAddress() : 0x00823804U;
}
static NV_INLINE NV_STATUS cmp70PortGetWrite(OBJGPU *pGpu, NvU8 *spec)
{
    NvU32 address = cmp70PortWriteAddress(), i;
    if (!cmp70PortEnabled(pGpu) || osCmp70PortMode() != 3 || !address)
        return NV_ERR_NOT_SUPPORTED;
    for (i = 0; i < 4; i++) { spec[i] = (NvU8)(address >> (8*i)); spec[4+i] = 0xff; }
    return NV_OK;
}
#endif

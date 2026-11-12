# Govee H6125 on-device effects: scene-param and DIY formats

Goal: make the strip itself store and play a user-defined effect, as the Govee app does,
instead of the PC streaming frames. Code: `govee_win11/diy_format.py`, tests:
`tests/test_diy_format.py`. Written 2026-11-09. Nothing in this document has been sent to the strip yet.

Confidence labels: **[V] verified from our data or hardware**, **[D] documented by others with code or
captures**, **[H] hypothesis**.

---

## 1. Transport

| Item | Format | Conf. |
|---|---|---|
| Command frame | `33 cmd payload… 00… xor` (20 bytes, xor of bytes 0–18) | [V] |
| A3 multi-packet | payload `01 <frame count> <subtype> <body…>`, cut into 17-byte chunks, each sent as `A3 <idx> <chunk>`, with idx = 0,1,2…. The last frame's idx is replaced by `FF`. If everything fits in one chunk, an empty `A3 FF` terminator is added and counted. | [V] for subtype 02 (our `multi_packets`); [D] for the rest |
| A3 subtype | `01` = sceneType-1 palette scene, `02` = layered scene/Workshop, `03` = paint ("graffiti") DIY, `04` = DIY Type 04 | [D] teh-hippo `native_scene_upload_route` uses `scene.scene_type` as the subtype; `effect_commands.py` |
| A1 multi-packet | `A1 02 00 <n data frames>` start; `A1 02 <i> <16 bytes>` for i = 1..n; `A1 02 FF` end | [D] H6127 capture (byte-exact in our tests); teh-hippo `transport.fragment_a1_02` |
| Scene select | `33 05 04 code_lo code_hi [scene_type]` | [V] (scene_type byte 0) |
| DIY select | `33 05 0a code_lo code_hi` | [D] H6127 capture, teh-hippo `build_h617a_diy_activation` |

**Bug found [D]+[V]:** `scenes.py` always sends subtype `02`. The two sceneType-1 effects in our
library (Halloween code 110 and Sweet code 107, both cmdVersion 0, params start `83`) should be sent with subtype `01`.
`diy_format.custom_scene_packets(param, code, scene_type=1)` does this correctly.

## 2. `scenceParam` format (catalogue scenes), decoded from our 240-variant library

### sceneType 2: layered (226 of the 228 non-empty params) [V]

```
<n_layers> ( <len> <layer body: len bytes> ) × n_layers
layer body:
  area           u8   high nibble = width in tenths (0 = full), low nibble = area index   [D semantics]
  select_type    u8   0 segment, 1 continuous ICs, 2 random ICs, 3 custom segment         [D]
  select_p1/p2   u8×2 quantity (BE u16), or random max/min, or piece/gap IC counts         [D]
  flags          u8   bit1 = brightness gradient (values seen: 0, 2)
  n_bright       u8   (1..3 seen)
  bright block × n_bright: scope_start, scope_end, order(0..3), SPEED, brightest_ret, darkest_ret
  direction      u8   bit7 = backwards, low nibble = distribution method (0..3 seen)
  colour_speed   u8   SPEED
  colour_ret     u8
  n_palette      u8   (1..11 seen)
  palette        RGB × n_palette
  selected_move  u8×3 packed(bit4 enabled, bit2 enter/exit, bits0-1 direction), interval, SPEED
  overall_move   u8×3 same layout
  priority       u8
```

Evidence: all 672 layers in the library parse with **zero leftover bytes** and the layer chain
ends exactly at the end of every param. `decode_scene_param` → `encode_scene_param` reproduces
**every** library param byte for byte (test). The field names and meanings come from
teh-hippo's Kaitai spec `govee_shared.ksy` (`effect_layer`).
Palettes match the scene names, e.g. Deep sea = `0064ff,0000ff` + `00ff96`, Fire = reds and oranges,
and Rainbow v1 = `ff0000,ff7f00,ffff00,00ff00,00ffff,0000ff`.

**cmdVersion 0 vs 1 [V]:** both versions use the same grammar. The differences are in content:
v1 has more layers, finer palettes and higher speed values. Use v1 on the H6125 (smoother, as
verified earlier).

### sceneType 1: palette steps (Halloween, Sweet) [V]+[D]
`config(bit7 brightness flag, bits4-6 layout, bits0-2 colour stride=3) n_steps (RGB u16le)×n n_pal RGB×n`.
Both examples round-trip exactly.

### Speed [V]
The H6125-specific `specialEffect[]` entries (161 entries with `supportSku` ∋ H6125) have the same
param as the main entry but `speedInfo.supSpeed=true`. Their `config` is a list of pages, one per
layer: `color` → colour_speed, `moveIn` → selected_move speed, `moveAll` → overall_move speed, and
`bright[{brightPage:i}]` → speed of bright block i. With `defaultIndex`, these arrays reproduce the
catalogue bytes in 470 of 474 fields. Every array is ascending (e.g. `[242,247,252,254]`).
Implemented in `apply_speed_config(param, config, index)`.
**[H]** a larger value means faster, and the useful range is about 200–255.
(`scenes.py` currently ignores `specialEffect`, so it never exposes this speed control.)

### Other fields
`diyEffectCode`, `diyEffectStr` and `rules` are empty for every H6125 variant [V].
In other SKUs' catalogues `diyEffectCode` names a native DIY family (e.g. 797/798 on H60A6) [D].

## 3. DIY "Type 04" body (what the app's DIY editor produces)

```
single: family variant speed(0..100) 3·n RGB×n                         (n = 1..8)
mixed : FF     00      speed(0..100) 3·n RGB×n 2·m (family variant)×m    (m = 1..4)   ("混合")
```

Sources [D]:
- BeauJBurroughs/Govee-H6127-Reverse-Engineering, a real capture. It shows the body after a
  1-byte selector. Our builder reproduces both published H6127 streams byte for byte, xor included.
- teh-hippo/ha-govee-led-ble `diy_type04.ksy` and `build_h617a_diy_single_body/multi_body`.

H6125 family/variant table (teh-hippo `H6125_DIY_FAMILIES`). Its order matches the Govee app's
DIY grid exactly (`docs/reference/govee-app-diy-effects.png`):

| App tile | key | family | variant | Conf. |
|---|---|---|---|---|
| 淡入淡出 1/2/3 | fade1-3 | 0 | 0 / 1 / 2 | [D] |
| 跳跃 1/2 | jump1-2 | 1 | 0 / 2 | [D] |
| 闪烁 1/2/3 | blink1-3 | 2 | 0 / 1 / 2 | [D] |
| 跑马灯 1/2/3 | marquee1-3 | 3 | 3 / 4 / 5 | [D] |
| 流水 1/2 | stream1-2 | 8 | 9 / 10 | [D] codes, [H] name mapping |
| 彩虹 1/2 | rainbow1-2 | 9 | 9 / 10 | [D] codes, [H] name mapping ("flow") |
| next tile | chase | 10 | 0 | [D] (max 3 colours) |
| music tiles | music1-3 | 4 | 8 / 6 / 7 | [D] |

Only families 0, 1, 2, 3, 8 and 9 can be mixed [D].

### Two upload routes on H6125 [D], chosen by controller revision
1. **A3 route** (pact type 10: hardware 2.01.x / 3.x, firmware 2.x/3.x; brightness on a 0–100 scale):
   send `a3_packets(0x04, body)` then `33 05 0a 18 00` (DIY code 24).
   On H6125 hw 3.01.10 / fw 3.04.37 / pact 10/2, owners confirmed teh-hippo's Effect Studio works.
   **The tested strip probably uses this route:** brightness 100 and 255 looked the same, and its
   cmdVersion-1 scenes play like the confirmed pact-10 capture (issue #117).
2. **A1 route** (pact type 1, hardware 1.x): send `a1_packets(bytes([0xFE]) + body)` then `33 05 0a fe 00`.

teh-hippo also waits for a notify acknowledgement after the upload, before sending the selector
(`diy_requires_upload_ack`). We use write-without-response, so leave about 100 ms before the
selector and keep to at most 25 frames/s.

Example ("orange blur" = fade3, `#ff5000,#f20d0d`, speed 50, A3 route):
```
a3 00 01 02 04 00 02 32 06 ff 50 00 f2 0d 0d 00 00 00 00 cf
a3 ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 5c
33 05 0a 18 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 24
```

## 4. Custom layered scenes ("advanced")
- **Edited catalogue scene [V transport, H playback]:** re-colour or re-time an official param and send
  it with the official scene's own code (`custom_scene_packets`). Only the bytes change; the
  transport and selector are the ones we have verified. teh-hippo ships this as "edited palette /
  layered scenes" for H6125 [D].
- **Advanced code [D]:** H6125 profile `advanced_diy_selector=402`. Send a layered body as A3
  subtype 02, then `33 05 04 92 01 00`. Workshop variant: code 401 with scene-type byte 2
  (`33 05 04 91 01 02`).
- teh-hippo's "native DIY" layered templates (501–507: Brilliant, Colorful Sky, Meteor, Meteor
  Shower, Shine, Bloom, Stack; seeds in `effect_catalogue.py`) are selected with `33 05 04 <code>` [D].

## 5. Other prior art checked
- homebridge-govee `ble-protocol.js`: lists `A1` = DIY data, `A3` = multi-packet scene,
  `33 05 0a` = DIY. Constants only, no body layout.
- lasswellt/govee-homeassistant `docs/govee-protocol-reference.md` §10: the H6127-style DIY outline
  (styles 0 fade, 1 jump, 2 flicker, 3 marquee, 4 music).
- Govee Open API `diyScene` and LAN `ptReal` both need a Wi-Fi model; H6125 is BLE-only, so neither helps here.

## 6. Probe plan (someone watching the strip; ranked by safety and information)
Before each probe, set all segments to one plain colour (e.g. dim white) so any change is visible.
Pace frames at no more than 25/s and wait about 150 ms before the final `33 05 …` selector.

1. **Recoloured official scene.** Take Aurora cmdVersion 1 (code 2289) from the library and send
   `custom_scene_from_template(effect, colors=[(240,64,176),(10,120,200),(10,16,176),(192,8,79)])`.
   If it works: Aurora's slow flow, but only pink and blue, with no green or white.
   Fallback: the strip plays normal Aurora (cached param), or nothing.
2. **DIY, A3 route:** `preset_packets("orange blur", "a3")`.
   If it works: the strip shows 淡入淡出3 in orange and red, like the app preset.
   If nothing changes, run 3.
3. **DIY, A1 route:** `preset_packets("orange blur", "a1")`. Same expected result. Whichever route
   works is the one to use from now on.
4. **Mix and the 彩虹 mapping:** `preset_packets("pink blue", <route>)`.
   Expected: matches the app's "pink blue". If it looks like 流水 instead, swap the family numbers
   8 and 9 in `DIY_EFFECTS`. Also try the single effects `diy_packets("rainbow1", cols, 45, route)`
   and `"stream1"`.
5. **Speed:** DIY speed 10 vs 90. Then a speed-capable scene from `specialEffect` with
   `apply_speed_config(..., index=0)` vs the top index.
   Expected: clearly slower or faster. Confirms that a larger byte means faster.
6. **Advanced code:** `advanced_packets(recoloured_aurora_param)` (402), then `workshop=True` (401).
   If either works, custom layered effects no longer depend on borrowing an official scene code.
7. **Persistence:** after 2 or 3 works, power-cycle the strip (`33 01 00` / `33 01 01`) and check
   whether the DIY keeps playing. Then disconnect the PC and check again. This shows whether the
   effect is truly stored on the device.

## Sources
- https://github.com/teh-hippo/ha-govee-led-ble (commit 8237ffab): `tools/ble/kaitai/govee_shared.ksy`, `scene_body.ksy`, `diy_type04.ksy`, `command_write.ksy`, `custom_components/ha_govee_led_ble/transport.py`, `effect_commands.py`, `effect_compiler.py`, `effect_catalogue.py` (H6125_DIY_FAMILIES, H617A_TYPE04_APPLY_CODE=24), `const.py` (H6125 profile: a1_diy_selector=0xFE, advanced_diy_selector=402), `native_scenes.py`
- H6125 confirmations: https://github.com/teh-hippo/ha-govee-led-ble/issues/447, https://github.com/teh-hippo/ha-govee-led-ble/issues/117
- https://github.com/BeauJBurroughs/Govee-H6127-Reverse-Engineering (README, DIY section)
- https://github.com/lasswellt/govee-homeassistant/blob/master/docs/govee-protocol-reference.md
- https://app.unpkg.com/@homebridge-plugins/homebridge-govee@11.24.0/files/lib/utils/ble-protocol.js
- https://github.com/egold555/Govee-Reverse-Engineering

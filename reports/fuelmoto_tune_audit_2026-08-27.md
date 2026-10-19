# Fuel Moto tune — audit report

**Tune:** `M8_131_CAM2_63inj fuelmoto origional.tbw`
**Origin:** Fuel Moto (Lucas), order #2417923, emailed 21 Jul 2026
**Analysed:** 27 Aug 2026 · SHA256 `2702c015…d24c54b3`

---

## Bottom line

This is a **clean, unmodified vendor base map** — and that is exactly the
problem. It has essentially never been adapted to your engine. The one
AutoTune session recorded against it changed **47 bytes out of 214,967**,
against ~1,252 for a normal session in your history. **Confirmed cause:
AutoTune is switched OFF.**

That is correct for a fresh base-map flash — your USB README deliberately says
to flash with it off — so nothing is broken. **The missing step is turning it
back on.** Until then the bike runs pure open-loop on Fuel Moto's calibration
and cannot correct a single fuelling error by itself.

Three things to act on, in order:

1. **Turn AutoTune ON**, then ride with CHT between 200–280 °F. This is the
   whole ballgame — it is the only mechanism on this bike that measures
   anything, and right now it is disabled.
2. **You have two different "current" tunes** on two different format
   versions — the one in your starred email, and the one your README says to
   flash first. Confirm which is actually on the bike before enabling
   anything.
3. **Your entire tuning history predates the S&S 550 cam.** All 70 NAS tunes
   were developed on a different camshaft, so their VE and timing numbers no
   longer describe this engine. Do not graft them onto this map.

---

## 1. Which file this is, and where it came from

The tune is in your **starred** email — *"131 tune 550 cam"*, 15 Aug, from
`joshairborne@icloud.com`, attachment **`M8_131_CAM2_63inj.tbw`**.

The two *forwards* of "Fw: Fuel Moto Map Support (Order #2417923)" (9 and
14 Aug, from `joshairborne@yahoo.com`) say *"I have attached your tune"* but
carry **no attachment** — 13 KB messages for a 214,967-byte file. The Yahoo
iPhone forward stripped it. That is worth knowing if you ever rely on those.

I audited the hash-verified USB copy of the same file
(`~/tmax-exchange/tunes-from-usb-2026-08-19/M8_131_CAM2_63inj fuelmoto
origional.tbw`, SHA256 `2702c015…`). **Caveat:** I could not download the
email attachment to compare byte-for-byte — the Gmail connector has no
attachment-download capability — so the match rests on the identical filename
stem and a message size consistent with that attachment, not on a hash. If you
want it certain, save the attachment into `~/tmax-exchange/` and I will hash
both.

## 2. You have FOUR different file-format versions, and two "current" tunes

**Correction to an earlier draft:** I first called this simply "a newer
format". It is more complicated, and the complication matters.

| File | Size | Version | Base map |
|---|---:|---|---|
| The 70 `THROTTLE LOGIC` tunes | 214,470 | `0x147` | various |
| `17AUG2025ATLRUNNINGGREATv6TODAY.tbw` | 214,470 | `0x147` | `ZZSSQXETDN100720` |
| **`M8_131_CAM2_63inj.tbw`** — your starred email | 214,967 | **`0x148`** | `HXSSEDCAAN061617` |
| **`M8_131_550CAM_63inj.tbw`** — "flash this first" | **225,718** | **`0x143`** | `HXSSEDCAAN061617` |

Note version does **not** track size, and `0x143` is *older* than `0x147`.
So this is not one clean "old vs new" split — there are at least three TBW
format generations in your possession.

**The important part: you have two different tunes for this build.**

- The tune in your starred email ("131 tune 550 cam", 15 Aug, attachment
  `M8_131_CAM2_63inj.tbw`) — this is the Fuel Moto original, and it is what
  this report audits. Message size 316 KB is consistent with a 214,967-byte
  attachment base64-encoded.
- `M8_131_550CAM_63inj.tbw`, which your USB `READ_ME_FIRST.txt` says is
  **"THIS is the file to flash first"**, and which `bike_profile.json` records
  as `current_flash_file`. It is a *different file*, 10,751 bytes larger, on a
  different format version.

**Confirm which of those two is actually flashed before changing anything.**
They are not interchangeable and they are not the same calibration.

### The USB README also changes the AutoTune finding

`READ_ME_FIRST.txt` (written 19 Aug) instructs, for the flash-first file:

> Confirm injector size says 6.3. Idle 1024. Decel cut OFF. **AutoTune OFF.**

**RESOLVED 27 Aug — Joshua confirms AutoTune is OFF.** So the near-zero
learning is the switch, not the heat gate. That is *correct* for a fresh
base-map flash — the README deliberately says to flash with it off — but it
means **the missing step is turning it back on.** Until that happens the bike
runs pure open-loop on Fuel Moto's map and cannot correct anything.

I established that the 497 extra bytes are inserted **early**, before the table
region, so table offsets are displaced by a constant **+497**. That was tested
against the alternatives (+497 beat both 0 and −497 at all four probe points:
92%, 72%, 81%, 71% agreement vs 88/43/63/34) and then **independently
corroborated**: mapping the changed bytes back through the shift lands them
squarely in the AutoTune and checksum bands, which is precisely where an
AutoTune run should write. A wrong shift would scatter them randomly.

**Caveat, stated plainly:** the shift is well-evidenced but not *proven*, and
per-cell engineering scaling is unconfirmed for this format as it is for the
old one. So I can tell you *where* and *how much* changed. I cannot yet tell
you "your AFR at 3000 rpm is 13.4" out of this file, and I will not guess it.

## 3. Your tuning history is obsolete — because of the cam, not the map

**Correction to an earlier draft of this report.** I first wrote that this was
a different base map from everything on the NAS. That was wrong: I sampled
three files and generalised to all seventy. The real distribution is three
base maps in clear succession:

| Base map | Tunes | Date range |
|---|---:|---|
| `H1SSTJLAAN120822` | 10 | 2025-09-26 → 09-30 |
| `HYSSPVCAHN051320` | 57 | 2025-10-03 → 10-22 |
| **`HXSSEDCAAN061617`** | 3 | 2025-10-24 → 11-18 |

The Fuel Moto tune is **`HXSSEDCAAN061617` — the same base map as your last
three tunes.** What changed is the file *format* (`0x147 → 0x148`), which
points to a ThunderMax software update somewhere between Nov 2025 and Jul 2026.

`currenttune.tbw` is dated **2025-10-22** — ten months old and on the older
`HYSSPVCAHN051320` map. It is a stale filename, not evidence of what is
flashed today.

**The finding that survives, and it is the important one:** you installed an
**S&S 550 cam** in the 131 in 2026. A cam change is a volumetric-efficiency
change — different lift and duration move how much air the engine actually
pumps at each rpm and throttle position. That means:

1. **All 70 tunes on the NAS predate the cam.** The 57-tune body of work on
   `HYSSPVCAHN051320` — the decel-pop protocol, the timing experiments, the
   "hopefullycooler" series — was developed for a *different camshaft*. The
   VE and fuel numbers in it no longer describe this engine.
2. **Even the 3 tunes on the current base map are pre-cam** (Oct–Nov 2025).
3. That is precisely why Fuel Moto issued a new calibration, and why starting
   from their map rather than porting your old cells is the correct move.
   **Do not graft the old tune's VE or timing cells onto this one.**

There is an **8-month gap** in the tune record: nothing since 2025-11-18. The
tuning effort has not restarted since the cam went in.

## 4. AutoTune learned almost nothing — because it was switched off

Comparing the Fuel Moto original against
`auto tune run from ch home 330 345 degrees f head temp.tbw` (same format,
same base map, so this diff is valid):

- **47 bytes changed**, 0.02 % of the file, across 18 small regions
- By band: `learned_ve_bulk` 18, `section_metadata_checksums` 8,
  `autotune_learned` 5, unmapped 12, header 4

For scale: a normal AutoTune session in your NAS history
(`automaprun → automaprun2`) moved **1,252 cells** in `autotune_learned`
alone. This one moved **5**.

**Confirmed cause: AutoTune was off.** The file is named for a 330–345 °F
head temp, and that reading is genuinely above the 200–280 °F learning window
— but the switch is the reason nothing was learned, not the temperature.

Two separate gates have to be satisfied before the ECM adapts anything:

1. **AutoTune enabled.** Currently off. This is the blocker.
2. **CHT inside 200–280 °F** while riding. Below is cold-start enrichment,
   above is heat soak; both are garbage as learning input.

On the temperature, for balance: an air-cooled M8 running 330–345 °F at the
head in traffic is high but not unusual for the platform, and the rear
cylinder always runs hotter. I am not going to tell you the engine is in
danger from a filename. What I *can* say is that at that temperature AutoTune
would not learn even once you switch it on — so both gates matter, in that
order.

**Do not lock in the trims from that file.** Whatever is in it was not learned
under valid conditions.

---

## Your questions, answered directly

**Is it a perfect tune?**
No — and it is not meant to be. It is a vendor *base map* for a 131/CAM2/6.3-
injector combination. It is a sane, professional starting point, not a
finished tune for your specific bike, and it has had essentially zero
adaptation to your engine.

**Is the motor running perfect temps?**
The only temperature evidence I have is a *filename* saying 330–345 °F, which
is not a measurement I can verify. Taking it at face value: that is high, and
it is above the 200–280 °F AutoTune learning window — but for an air-cooled
M8 in traffic it is not unusual for the platform, and the rear cylinder always
runs hotter than the front. **I am not going to tell you your engine is in
danger on the strength of a filename.** What matters practically is that at
that temperature AutoTune will not learn even after you switch it on. Get a
real CHT log and this stops being guesswork.

**Is decel pop gone?**
**Unknowable from a tune file, and I will not guess.** A `.tbw` records what
the ECM is *commanded* to do, never what the engine *did*. There is no ride
datalog anywhere in this project or on the NAS — 153 tunes, 33 PDFs, 4 CSVs,
and the CSVs are recommendation tables, not recordings. The only way to answer
this is to ride the protocol: closed-throttle 4000→2000 rpm decels in 3rd and
4th, logging TPS, RPM, AFR target vs actual, and CHT.

**What will the bike be doing because of this tune?**
Running the Fuel Moto calibration for a 131 / CAM2 / 6.3-injector combination,
with no meaningful learned correction on top of it. It should run, and run
safely, because it is a vendor map. It will not be optimised for your exhaust,
your altitude, or your riding — that is what AutoTune was supposed to do, and
it could not.

---

## What I would do next, in order

1. **Settle which of your two current tunes is actually flashed** —
   `M8_131_CAM2_63inj.tbw` (email, format `0x148`) or
   `M8_131_550CAM_63inj.tbw` (README's "flash this first", format `0x143`).
   They are different calibrations on different format versions. Also confirm
   in TMax Tuner: injector size 6.3, idle 1024, decel cut OFF, and whether
   AutoTune is on or off.
2. **Fix the heat before chasing the tune.** AutoTune cannot help while the
   heads sit at 330–345 °F. Run `tmax fix "running hot"` for the
   guardrail-checked options — but note the first check there is a cooling
   ride, not a map edit.
3. **Get one clean AutoTune session inside 200–280 °F.** That is worth more
   than any map edit I can recommend, because it is the only thing on this
   bike that actually measures anything.
4. **Export one ride datalog.** It is the single biggest unblock for this whole
   assistant — it turns every recommendation from a hypothesis into something
   measurable.
5. **Ask Lucas to resend the tune** directly, so the original is hash-verified
   rather than inferred from a USB copy.

---

*Read-only audit. No `.tbw` file was written or modified. Every number above
came from the bytes; where scaling is unconfirmed I said so rather than
estimating.*

# usphere Trapping Protocol

**Repository:** usphere-FPGA (usphere-control)
**Document Type:** Operational guide and software reference
**Status:** Living document — updated as each stage is implemented

---

## Overview

Optically trapping a microsphere in high vacuum is a multi-stage procedure that begins at atmospheric pressure and ends with a particle stably confined in the focus of a 1064 nm laser, feedback-cooled, and charge-neutralized at pressures below 10⁻³ mbar.  The procedure currently requires interacting with several independent pieces of hardware through separate software interfaces (LabVIEW VI, evaluation software, and this FPGA control script).  The goal of this codebase is to automate the entire chain from a single interface.

The stages in order are:

1. **Load spheres onto the dropper** — deposit microspheres onto the underside of a glass slide that will serve as the dropper mechanism (manual)
2. **Install the dropper in the chamber** — mount the slide assembly above the vacuum chamber and seal (manual)
3. **Pump down to loading pressure** — rough-pump the chamber to ~2–3 mbar where the residual gas provides enough viscous damping to capture a falling sphere
4. **Position the dropper over the laser beam** — use a piezo linear actuator to translate the glass slide into alignment with the trap beam axis
5. **Shake the dropper to release spheres** — drive a piezo attached to the slide with a custom waveform to dislodge spheres so they fall through the beam
6. **Confirm a sphere is trapped** — monitor the balance photodiode signal (FPGA) and camera for the characteristic jump in scattered light that indicates a trapped sphere
7. **Retract the dropper** — move the actuator to withdraw the slide from the beam path
8. **Lower the sphere into the trap focus** — adjust the z-axis laser focus or electrode position to bring the sphere to the optimal trapping point
9. **Enable feedback and lock the sphere** — set parametric/active feedback gains on the FPGA and verify stable confinement
10. **Initial charge neutralization (~2–3 mbar)** — drive the sphere toward net-zero charge using the UV lamp and electrodes; single-charge resolution is not yet available at this pressure
11. **Pump down to high vacuum** — open the valve to the turbo pump foreline, engage the turbo pump when pressure reaches ~0.1 mbar, and wait for the chamber to stabilize
12. **Final charge neutralization (high vacuum)** — with the sphere at high vacuum and high Q, resolve and correct single-electron charge changes to reach true net-zero charge

The sections below describe each stage in detail, including the hardware involved, the software module or procedure that automates it, and notes for the operator.

---

## Stage 1: Load Spheres onto the Dropper

**Hardware:** Glass slide, dropper assembly, microsphere suspension (SiO₂, ~3–20 µm diameter)
**Automation:** None — manual step
**Script:** N/A

Microspheres are deposited onto the underside of a glass slide using a dropper or pipette.  The slide is then inverted and installed in the dropper assembly.  The density of spheres on the slide affects how many loading attempts are needed before a sphere is captured; too few and trapping takes a long time, too many and multiple spheres may be captured simultaneously.

*This section will be expanded with specific slide preparation notes.*

---

## Stage 2: Install the Dropper in the Chamber

**Hardware:** Dropper assembly, vacuum chamber, feedthroughs
**Automation:** None — manual step
**Script:** N/A

The loaded slide assembly is installed in the chamber above the trapping region.  Electrical feedthroughs for the shaker piezo are connected.  The chamber is then sealed and the roughing pump is turned on.

*This section will be expanded with chamber assembly notes.*

---

## Stage 3: Pump Down to Loading Pressure

**Hardware:** Foreline (roughing) pump, Edwards TIC pressure controller, Pirani / wide-range gauges
**Automation:** Planned — `procedures/proc_pumpdown_rough.py`
**Module:** `modules/mod_edwards_tic.py`

The foreline pump runs continuously.  With the chamber sealed, pressure drops from atmosphere to ~2–3 mbar within a few minutes.  This pressure range is optimal for the initial trapping step because the residual gas damps the sphere's kinetic energy on the way down, greatly increasing the probability of capture.

Pressure is monitored via the Edwards TIC (Turbo Instrument Controller) over RS-232.  The TIC reads a Pirani gauge (APGX, input 2) and a wide-range gauge (WRG, input 1) and returns pressures in mbar.

The target pressure for proceeding to the next stage is **2–3 mbar**.

*This section will be updated when the TIC module and pumpdown procedure are implemented.*

---

## Stage 4: Position the Dropper Over the Laser Beam

**Hardware:** Thorlabs Z812 linear actuator, Thorlabs KCube DC Motor Controller (KDC101), dropper chassis assembly
**Automation:** `procedures/proc_position_dropper.py`
**Module:** `modules/mod_dropper_stage.py`

### Dropper assembly construction

The dropper is a custom assembly.  A thin glass strip cleaved from a microscope slide is used as a cantilevered springboard.  A PA4CEW 2×2×2 mm, 150 V piezo stack chip (Thorlabs) is epoxied to the glass strip using 353NDPK high-temperature, high-vacuum-compatible epoxy (also Thorlabs).  The key requirements are a small piezo stack and an epoxy rated for high vacuum and bake-out temperatures — the specific product choices above satisfy these but substitutes that meet the same requirements are acceptable.

The glass strip with its epoxied piezo is installed in a custom aluminum chassis.  The chassis surrounds the dropper from below except at the far end, which has an aperture of approximately 1 mm diameter.  Microspheres are deposited on the underside of the tip of the glass strip; to fall into the trapping beam they must pass through this aperture.  The tight aperture geometry is what makes positioning straightforward: alignment is achieved when the aperture is centered on the beam axis.

The chassis assembly is mounted on a translation stage.  The stage is also fitted with a glass coverslip positioned below the trap focus.  The coverslip prevents any spheres that miss the trap from falling directly onto the collection optics below.

### Translation stage hardware

The translation stage is driven by a **Thorlabs Z812** motorized linear actuator (12 mm travel) controlled by a **Thorlabs KDC101 KCube DC Servo Motor Controller**.  The KDC101 communicates over USB using the Thorlabs Kinesis SDK.  The Python interface uses [pylablib](https://pylablib.readthedocs.io/) (`pip install pylablib`) with the Kinesis DLLs installed on the host.

**Module:** [`modules/mod_dropper_stage.py`](modules/mod_dropper_stage.py)
**Serial number (lab unit):** 27006288

The module exposes three named presets (editable in the GUI, stored in `modules/dropper_stage_state.json`):

| Preset | Default position | Description |
|---|---|---|
| `retrieval` | 5.0 mm | Stage position for physically accessing or replacing the dropper assembly |
| `dropping` | 6.5 mm | Aperture aligned with both beams; spheres fall into trap |
| `retraction` | 11.0 mm | Coverslip clear of trapping beams; used after a sphere is confirmed trapped |

These positions are stable across sessions and are typically unchanged unless the dropper assembly is replaced, in which case `dropping` may shift by ~1 mm.  The last commanded position is recorded to `dropper_stage_state.json` on every move so the GUI can display it on next boot (before homing resets the encoder).

**Motor parameters:** Default velocity 1 mm/s, acceleration 1 mm/s².  A config value of 0.0 for any motion parameter means "keep whatever is stored in the KDC101" — the controller remembers its last Kinesis-configured values in non-volatile memory, so the first time the module is used all motion parameters can be left at 0.0 and the device's factory/Kinesis defaults apply.

**Available commands:** `home`, `move_to` (absolute mm), `move_to_preset` (`retrieval` / `dropping` / `retraction`), `jog` (forward or reverse, configurable step size).

### Positioning procedure and optical feedback

With the chamber at loading pressure, the stage is driven inward (toward the beam axis) from its fully retracted position.  The correct dropper position is identified optically using the 532 nm alignment laser, which is co-aligned with the 1064 nm trapping beam.

The 532 nm beam is detected outside the chamber on the same balanced photodiodes used for x/y position sensing of a trapped sphere.  The 532 signal provides three distinct states as the stage moves:

| Stage position | 532 signal on BPD | Meaning |
|---|---|---|
| Fully retracted | Strong | No obstruction; open beam path |
| Chassis blocking aperture | Near zero | Chassis wall is in the beam |
| Aperture aligned with beam | Moderate (attenuated) | Beam passes through coverslip, aperture, and sphere-coated glass tip |

The attenuation in the aligned state is real and expected: the beam passes through the coverslip (which may carry a light dusting of spheres despite cleaning efforts) and through the end of the glass strip, which has microspheres deposited on it.  The moderate signal is nonetheless clearly distinguishable from the zero-signal (blocked) state and from the strong signal (fully retracted) state.

The same signature is visible on the camera.  In practice, the correct position is saved as a named preset (in actuator encoder counts) from a previous run.  The procedure drives to the saved position and the operator confirms using the live photodiode readback.

**Why this matters:** If the aperture is not aligned with the beam, spheres cannot fall into the trap regardless of how aggressively the shaker piezo is driven.  Even sub-millimeter misalignment is enough to block all spheres at the aperture.  The optical feedback makes alignment unambiguous.

---

## Stage 5: Shake the Dropper to Release Spheres

**Hardware:** Shaker piezo (attached to slide), arbitrary waveform generator (AWG), power amplifier
**Automation:** Planned — `procedures/proc_shake_dropper.py`
**Module:** `modules/mod_shaker_awg.py`

Once the slide is positioned, a shaker piezo attached to the glass slide is driven with a custom waveform generated by a dedicated AWG and amplified before being sent to the piezo.  The waveform amplitude, frequency content, and duration are tunable.  The mechanical agitation dislodges spheres from the slide so that they fall through the beam.

The shaker is driven in bursts while the operator (or the trap-detection procedure) monitors for a trapping event.  Parameters such as waveform shape, amplitude, and burst duration are stored as presets and tuned to the specific sphere size and loading density.

*This section will be expanded with AWG model, communication interface, and waveform design notes.*

---

## Stage 6: Confirm a Sphere is Trapped

**Hardware:** Balanced photodiodes (BPD, x and y axes), camera, FPGA
**Automation:** Planned — live monitor within `proc_shake_dropper.py`
**FPGA registers:** BPD amplitude indicators (read-only)

While the shaker is running, the dropper aperture is aligned with the beam (Stage 4) and the 532 nm alignment laser is transmitting a moderate signal through the assembly.  A sphere falling through the aperture and entering the trap focus produces a characteristic change in the BPD signal.  The sphere scatters the 532 nm probe beam and the trapping 1064 nm beam, and its presence is visible on both the photodiodes and the camera.

The detection context is important: during the shaking phase the 532 signal is already attenuated (beam is passing through the dropper assembly), so the trapping event appears as a further change in signal character — not a simple threshold crossing on an otherwise quiet baseline.  The exact signature depends on sphere size and trap depth and will be characterised when the detection logic is implemented.

The balanced photodiodes read x and y position of scattered 532 nm light; the FPGA records the in-loop BPD signal.  The procedure monitors the relevant FPGA indicator registers in `on_fpga_update()` and flags a trapping event when the signal change exceeds a configurable threshold or exhibits a recognisable pattern.

The camera provides independent confirmation — the sphere appears as a bright spot at the trap focus and its presence can be confirmed visually or via a simple thresholded frame-difference.  Camera integration is planned as an optional live-view panel within the procedure tab.

*Threshold values, indicator register names, and the precise detection signature will be documented here when the detection logic is implemented.*

---

## Stage 7: Retract the Dropper

**Hardware:** Thorlabs Z812 / KDC101 (same as Stage 4)
**Automation:** `procedures/proc_position_dropper.py` (retract preset)
**Module:** `modules/mod_dropper_stage.py`

After a sphere is confirmed trapped, the stage is driven back to its fully retracted position.  This accomplishes two things:

1. **Removes the dropper from the optical path** — the glass tip, the sphere deposit on it, and the chassis aperture are all withdrawn, eliminating any scattering or clipping of the 532 nm and 1064 nm beams that would interfere with position detection of the trapped sphere.
2. **Removes the coverslip** — the coverslip mounted on the translation stage is pulled out from below the trap focus.  Any spheres that settled on the coverslip during loading are removed, and the coverslip itself no longer contributes any optical background to the detection beams.

The retract position is a named preset (encoder counts).  Completion is confirmed when the 532 nm signal on the BPD returns to its strong, unattenuated level — i.e., the open-beam condition is restored.

---

## Stage 8: Lower the Sphere into the Trap Focus

**Hardware:** FPGA (z-axis feedback / laser power)
**Automation:** Planned — `procedures/proc_lower_to_focus.py`
**FPGA registers:** Z-axis setpoint, laser power AOM

After the dropper is retracted, the sphere may not be at the optimal axial position within the trap.  This stage adjusts the laser power or the electrode axial bias to bring the sphere to the focus of the trap where the gradient force is maximum and feedback is most effective.

*Register names and ramping parameters will be documented here when the procedure is implemented.*

---

## Stage 9: Enable Feedback and Lock the Sphere

**Hardware:** FPGA (parametric / active feedback on X, Y, Z)
**Automation:** Planned — `procedures/proc_enable_feedback.py`
**FPGA registers:** Feedback gains, notch filter coefficients, PID setpoints

Parametric and/or active feedback is engaged to damp the sphere's center-of-mass motion.  The FPGA implements the feedback digitally; gain parameters are set via the `change_pars()` interface in `fpga_core.FPGAController`.

The procedure ramps gains from zero to their target values to avoid exciting transients, then verifies that the oscillation amplitude (read from FPGA indicators) decreases to within the target range.

*Gain ramp sequences and stability criteria will be documented here when the procedure is implemented.*

---

## Stage 10: Initial Charge Neutralization (~2–3 mbar)

**Hardware:** UV flash lamp, electrodes, lock-in amplifier
**Automation:** External — [`usphere-charge-control`](https://github.com/your-org/usphere-charge-control) (git submodule)
**Module:** git submodule at `resources/usphere-charge-control`

Charge management — measuring the sphere's net charge via an oscillating electric field detected by the lock-in amplifier, and neutralizing it with UV photoelectrons — is handled by a dedicated repository (`usphere-charge-control`) that already communicates with the correct instruments.  That repo is included here as a git submodule and exposed as a procedure tab rather than re-implemented.

At 2–3 mbar the Q factor is too low to resolve single electron changes, so the goal here is to reduce the charge to a small number of electrons (|Q| ≲ ~50e) before pumping to high vacuum.

---

## Stage 11: Pump Down to High Vacuum

**Hardware:** Turbo pump, turbo pump controller, foreline pump, valve, Edwards TIC
**Automation:** Planned — `procedures/proc_pumpdown_turbo.py`
**Modules:** `modules/mod_edwards_tic.py`, `modules/mod_turbo_pump.py`, `modules/mod_foreline_valve.py`

With the sphere stably trapped and roughly neutralized, the chamber is pumped to high vacuum.  The procedure:

1. Opens the valve connecting the chamber to the turbo pump foreline
2. Monitors the Pirani gauge; when pressure falls below ~0.1 mbar, enables the turbo pump controller
3. Monitors the wide-range gauge as the turbo spins up; waits for the pressure to stabilize in the target range (< 10⁻³ mbar, typically < 10⁻⁵ mbar for science runs)

The sphere must remain trapped and feedback-controlled throughout this stage.  If the sphere is lost, the procedure halts and alerts the operator.

*Module communication interfaces and valve/turbo controller details will be documented here when implemented.*

---

## Stage 12: Final Charge Neutralization (High Vacuum)

**Hardware:** UV flash lamp, electrodes, lock-in amplifier
**Automation:** External — `usphere-charge-control` submodule

At high vacuum the mechanical Q is very high and individual electron charge changes are detectable as discrete jumps in the sphere's oscillation frequency (as measured by the lock-in amplifier).  The sphere is driven to exactly zero net charge by iterating the UV flash and charge measurement.  This is the starting condition for precision science data acquisition.

---

## Software Architecture

The automation procedures in this repository follow a two-layer pattern:

**Hardware modules** (`modules/`)
: Thin, stateless instrument drivers.  Each module is a Python file exposing `MODULE_NAME`, `DEVICE_NAME`, `CONFIG_FIELDS`, `DEFAULTS`, `read(config)`, and `test(config)` (and optionally `command(config, **kwargs)`).  They are analogous to the device plugins in `usphere-daq` (e.g., `daq_edwards_tic.py`).

**Control procedures** (`procedures/`)
: Stateful PyQt5 widgets that orchestrate hardware modules and FPGA register operations to carry out a stage of the trapping protocol.  Each procedure subclasses `ControlProcedure` (see `procedures/base.py`) and receives a live `FPGAFacade` instance.  The `on_fpga_update(state)` callback fires each FPGA monitor cycle and is used for live detection logic.

The `FPGAController` in `fpga_core.py` is the authoritative interface to the FPGA hardware; procedures never import it directly — they receive a `LiveFPGAFacade` wrapper injected at load time.

---

*Last updated: 2026-04-16 — Stage 4 updated with Z812/KDC101 module detail; mod_dropper_stage.py implemented*

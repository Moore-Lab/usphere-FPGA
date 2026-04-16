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

**Hardware:** Piezo linear actuator (dropper stage), actuator controller
**Automation:** `procedures/proc_position_dropper.py`
**Module:** `modules/mod_dropper_stage.py`

With the chamber at loading pressure, the glass slide must be translated laterally so that the sphere-coated region is directly above the laser beam waist.  This is done with a piezo linear actuator that moves the dropper assembly along one axis.

The operator sets a target position (in the actuator's native units) and the procedure drives the stage to that position.  A live readback of the current position is displayed in the procedure tab.  The correct position is typically known from a previous trapping run and saved as a named preset.

**Why this matters:** If the slide is not correctly positioned the spheres fall too far from the beam axis to be captured, regardless of how aggressively the shaker is driven.  Even a fraction of a millimeter misalignment can reduce the trapping rate substantially.

*Details of the actuator hardware, communication interface, and position calibration will be added here as the module is implemented.*

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

**Hardware:** Balance photodiode (BPD, in-loop), camera
**Automation:** Planned — live monitor within `proc_shake_dropper.py`
**FPGA registers:** BPD amplitude indicators (read-only)

A trapped sphere scatters strongly from the trapping beam and produces a characteristic jump in the balance photodiode signal recorded by the FPGA.  The procedure monitors the relevant FPGA indicator registers in `on_fpga_update()` and flags a trapping event when the signal exceeds a configurable threshold.

The camera provides a secondary confirmation — the sphere is visible as a bright spot at the trap focus.  Camera integration is planned as an optional live-view panel within the trapping procedure.

*Threshold values and indicator register names will be documented here when the detection logic is implemented.*

---

## Stage 7: Retract the Dropper

**Hardware:** Piezo linear actuator (same as Stage 4)
**Automation:** `procedures/proc_position_dropper.py` (retract preset)
**Module:** `modules/mod_dropper_stage.py`

After a sphere is confirmed trapped, the glass slide is retracted so it no longer obstructs the beam path or the detection optics.  The retract position is a named preset in the dropper stage module.  The procedure moves the stage to this position and confirms arrival.

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

*Last updated: 2026-04-16*

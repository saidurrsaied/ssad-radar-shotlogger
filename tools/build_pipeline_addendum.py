#!/usr/bin/env python3
"""Generate training/Pedestrian_Radar_Pipeline_AsBuilt.pdf.

A companion to Pedestrian_Radar_Pipeline.pdf, which was written on 2026-09-21
when the embedded half was still a specification. Sections 1-4 of that document
(the idea, the data, feature extraction, training) remain accurate; its section
5 described what the device *should* do, and this describes what it does.

The first document's generator was lost with a session scratchpad. This one
lives in the repo so that cannot happen twice. Run it in a scratch venv --
ReportLab must not go into .venv:

    python3 -m venv /tmp/venv-doc && /tmp/venv-doc/bin/pip install reportlab
    /tmp/venv-doc/bin/python tools/build_pipeline_addendum.py

Only built-in fonts are used, so there is no font hunt and no missing-glyph
boxes. That means no Unicode arrows, dashes or Greek: write "->", "-" and "us".
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "training" / "Pedestrian_Radar_Pipeline_AsBuilt.pdf"

INK = colors.HexColor("#1a1c22")
MUTED = colors.HexColor("#5b6070")
RULE = colors.HexColor("#c9ccd6")
ACCENT = colors.HexColor("#1f6f8b")
BAD = colors.HexColor("#9c3b30")
PANEL = colors.HexColor("#f2f4f8")

styles = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=styles["Normal"], fontName="Times-Roman",
                      fontSize=10.2, leading=14.6, textColor=INK, alignment=TA_LEFT,
                      spaceAfter=7)
H1 = ParagraphStyle("h1", parent=BODY, fontName="Helvetica-Bold", fontSize=15,
                    leading=19, spaceBefore=16, spaceAfter=8, textColor=INK)
H2 = ParagraphStyle("h2", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5,
                    leading=15, spaceBefore=11, spaceAfter=5, textColor=ACCENT)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=9, leading=12.4, textColor=MUTED)
CODE = ParagraphStyle("code", parent=BODY, fontName="Courier", fontSize=8.6,
                      leading=11.6, textColor=INK, spaceAfter=2, spaceBefore=2)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=9, leading=12.2, spaceAfter=0)
CELLB = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")


def header(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(20 * mm, A4[1] - 12 * mm,
                      "Pedestrian detection with the A121 radar - the system as built")
    canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 12 * mm, "SSAD - FH Dortmund")
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(20 * mm, A4[1] - 14 * mm, A4[0] - 20 * mm, A4[1] - 14 * mm)
    canvas.drawCentredString(A4[0] / 2, 12 * mm, str(doc.page))
    canvas.restoreState()


def table(rows, widths, head=True):
    data = []
    for i, row in enumerate(rows):
        style = CELLB if (head and i == 0) else CELL
        data.append([Paragraph(c, style) for c in row])

    t = Table(data, colWidths=widths, hAlign="LEFT")
    cmds = [("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE)]
    if head:
        cmds += [("BACKGROUND", (0, 0), (-1, 0), PANEL),
                 ("LINEBELOW", (0, 0), (-1, 0), 0.8, MUTED)]
    t.setStyle(TableStyle(cmds))
    return t


def code(lines):
    """Preformatted drops whitespace-only lines, so blanks become Spacers."""
    out = []
    for line in lines:
        if line.strip():
            out.append(Paragraph(line.replace(" ", "&nbsp;"), CODE))
        else:
            out.append(Spacer(1, 5))
    return out


def build():
    doc = BaseDocTemplate(str(OUT), pagesize=A4,
                          leftMargin=20 * mm, rightMargin=20 * mm,
                          topMargin=20 * mm, bottomMargin=18 * mm,
                          title="Pedestrian detection with the A121 radar - as built",
                          author="SSAD, FH Dortmund")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=header)])

    s = []
    s.append(Paragraph("The system as built", H1))
    s.append(Paragraph(
        "A companion to <i>Pedestrian detection with the A121 radar</i> (21 September 2026). "
        "That document's sections 1 to 4 - the idea, the data, the feature extraction and the "
        "training - are unchanged and still correct. Its section 5 described an embedded pipeline "
        "that had not been written, on a toolchain that was not installed, over a link that was "
        "not decided. All three now exist and have run. This records what was built, what it "
        "measures, and where the specification turned out to be wrong.", BODY))

    s.append(Paragraph("1  Status", H1))
    s.append(table([
        ["Stage", "State", "Where"],
        ["Feature extraction (Python)", "Done. 8 unit tests pin its behaviour", "training/features.py"],
        ["Binary model", "Done. Trained, evaluated as int8, exported", "training/out/final/"],
        ["XM125 firmware", "Done. Streams raw frames, 65 KB of 128 KB flash",
         "firmware/xm125_streamer/"],
        ["Feature extraction (C)", "Done. Matches Python to 8e-6 dB on device",
         "firmware/radar_infer/"],
        ["Inference on device", "Done. ~32 ms per window, 24 % of one core", "firmware/radar_infer/"],
        ["Display", "Done. 480x480, status and scrolling spectrogram", "firmware/radar_infer/"],
    ], [58 * mm, 68 * mm, 44 * mm]))

    s.append(Paragraph("2  The shape of the built system", H1))
    s.append(Paragraph(
        "The radar module senses and streams; the ESP32-S3 computes and displays. Nothing else is "
        "in the loop - no PC, no protocol library, no handshake.", BODY))
    s.extend(code([
        "  A121 --SPI-- XM125 (Cortex-M4, STM32L431CB)",
        "                 |  raw Sparse IQ frames, 5632 B, 25 fps",
        "                 |  UART 2 Mbaud, 141 kB/s, ONE WIRE + GND",
        "                 v",
        "              ESP32-S3  frame -> column -> 64-column window",
        "                        -> int8 CNN -> CLEAR / PEDESTRIAN",
        "                        -> 480x480 panel",
    ]))
    s.append(Paragraph(
        "<b>Why the module does no signal processing.</b> Moving the spectrogram onto the M4 would "
        "cut the link to 1.6 kB/s, but it would clone features.py into a second toolchain, and that "
        "copy would need its own proof of agreement with the Python. The DSP that already matches "
        "NumPy exactly stays in one place. The module stays deliberately dumb.", BODY))
    s.append(Paragraph(
        "<b>Why inference cannot live on the module.</b> Arithmetic, not preference. The STM32L431CB "
        "has 128 KB of flash and 64 KB of RAM; the stock exploration server already occupies 109,168 "
        "bytes of that flash, 83 %. TensorFlow Lite Micro's runtime alone is several times what is "
        "left. The ESP32-S3 boots with 388 KB of free internal heap.", BODY))

    s.append(Paragraph("3  The link is one-directional, and that is deliberate", H1))
    s.append(Paragraph(
        "Only UART_TX and GND are wired. The ESP32's transmit pin is left unconnected because the "
        "XE125's own CP2105 USB bridge already drives that net, and the bridge must stay powered - "
        "the module needs a 1.8 V rail the jumper does not carry. Two transmitters on one net is "
        "contention.", BODY))
    s.append(Paragraph(
        "Three consequences follow, and each one bit during bring-up:", BODY))
    s.append(table([
        ["Consequence", "Why"],
        ["The firmware streams autonomously from boot",
         "It can never be told to start. The exploration server's own startup waits for the host to "
         "release the RX line; copying that would have produced firmware that never starts."],
        ["Hardware flow control is disabled",
         "The module would otherwise pause when CTS is de-asserted - and with no process holding the "
         "bridge's port open, the driver may leave it de-asserted for ever."],
        ["PC captures at 2 Mbaud are lossy by design",
         "About a fifth of every frame, in 256-byte USB chunks. Build with FLOW_CONTROL=1 when a "
         "complete capture is needed."],
    ], [56 * mm, 114 * mm]))

    s.append(Paragraph("4  The wire format", H1))
    s.extend(code([
        "  A1 21 F0 0D | 64 sweeps x 22 range points of { int16 real, int16 imag }",
        "              = 4 + 5632 bytes, 25 times a second",
    ]))
    s.append(Paragraph(
        "The magic word exists only so a receiver that loses synchronisation recovers on the next "
        "frame instead of emitting garbage indefinitely; the sensor provides no natural delimiter. "
        "64 x 22 x 4 x 25 = 141 kB/s against a 2 Mbaud ceiling, about 77 % utilised. Measured: "
        "141.9 kB/s, 25.0 fps, every frame intact. There is no headroom for a higher frame rate, "
        "which is one more reason the configuration stays frozen.", BODY))
    s.append(Paragraph(
        "tests/test_firmware_protocol.py fails if the two firmwares and tools/replay_frames.py ever "
        "stop agreeing on this format, or if the C configuration stops matching "
        "config/session_config.json.", SMALL))

    s.append(Paragraph("5  What it measures", H1))
    s.append(Paragraph("Numbers from the device, not from a model of it.", SMALL))
    s.append(table([
        ["Measurement", "Result", "Meaning"],
        ["Boot self-test vs features.py", "8e-6 dB worst, 0/64 int8 differ",
         "The C port reproduces the Python exactly"],
        ["Test vectors replayed", "q=127 and q=-125, both exact",
         "End to end agreement with the trained model"],
        ["DSP", "1.55 ms per frame", "4 % of the 40 ms frame budget"],
        ["Inference", "~32 ms per window", "20 % of the 160 ms window budget"],
        ["Tensor arena", "48,668 B measured", "The specification guessed ~25 KB"],
        ["Live empty room", "124 windows, 0 false positives", "Max p 0.035 against a 0.5 threshold"],
        ["Live walk", "98 of 110 windows positive", "Detection 0.3 s after the first positive"],
    ], [50 * mm, 52 * mm, 68 * mm]))
    s.append(Paragraph(
        "The empty-room run matters more than the walk. Until it was made, the detector had only "
        "ever been shown to reject <i>recorded</i> backgrounds.", BODY))

    s.append(Paragraph("6  The display", H1))
    s.append(Paragraph(
        "480x480, driven directly rather than through LVGL: the board support package's own "
        "benchmark puts LVGL near 97 % CPU, and the detector needs a quarter of a core. Writing one "
        "column per frame costs a 640-byte transfer.", BODY))
    s.extend(code([
        "  y   0.. 99   CLEAR / PEDESTRIAN, probability bar, threshold tick",
        "  y 100..419   spectrogram: 64 Doppler bins x 5 px, 480 columns = 19.2 s",
        "  y 420..479   APPROACH / DEPART axis",
    ]))
    s.append(Paragraph(
        "The spectrogram draws the int8 values <i>after</i> normalisation and quantisation - the "
        "literal bytes handed to the network. A separately scaled view would look better and would "
        "hide exactly the failure worth seeing. The cursor crosses in 19.2 s: that is the 25 Hz "
        "frame rate, not the 5500 Hz sweep rate, which resolves velocity within each frame.", BODY))

    s.append(Paragraph("7  Where the specification was wrong", H1))
    s.append(table([
        ["Section 5 said", "It is actually"],
        ["Arena roughly 25 KB of activations", "48,668 B, measured and logged at startup"],
        ["Flashed over USB DFU", "The STM32 UART bootloader. The button drives BOOT0; the module's "
         "own USB never reaches the XE125 connector"],
        ["The XM125 is a Cortex-M33", "A Cortex-M4, STM32L431CB"],
        ["The ESP32 reads UART0 / GPIO44", "UART1 on GPIO4. On the LCD-EV-Board almost every pin is "
         "taken by the panel; GPIO4 drives only the status LED"],
        ["Inference cost unknown", "~32 ms, and it rose from 26.4 ms when the display was added - "
         "the RGB panel competes for PSRAM"],
    ], [58 * mm, 112 * mm]))

    s.append(Paragraph("8  What is still not true", H1))
    s.append(Paragraph(
        "<b>All 230 recordings come from one room.</b> The project brief asks for two locations and "
        "that requirement is unmet. Claim generalisation to a new <i>person</i> - the evaluation is "
        "leave-one-subject-out - and not to a new <i>place</i>. A corridor test showed the detector "
        "still works in a geometry it has never seen, which is encouraging and is not evidence.", BODY))
    s.append(Paragraph(
        "<b>It is a motion detector at low SNR.</b> The campaign recordings sit close to the noise "
        "floor and show no static clutter at all, unlike an earlier pilot with the same "
        "configuration. Both false alarms in the leave-one-subject-out evaluation were doors. No "
        "fan or trolley in the dataset was ever visible to it.", BODY))
    s.append(Paragraph(
        "<b>The highest-value next work is a second location</b>, recorded under the same frozen "
        "configuration - not a different architecture.", BODY))

    s.append(Paragraph("9  Where everything is", H1))
    s.append(table([
        ["Path", "What"],
        ["firmware/xm125_streamer/", "The module firmware and its build script"],
        ["firmware/radar_infer/", "The ESP32-S3 firmware: DSP, inference, display"],
        ["training/features.py", "The reference DSP - the contract with the C"],
        ["training/out/final/", "Model, int8 tflite, C array, constants, test vectors"],
        ["tools/replay_frames.py", "Replays recordings or captures into the device"],
        ["HARDWARE_SETUP.md", "Build, flash, wire, verify - and the traps"],
        ["TOOLCHAIN_SETUP.md", "Why each tool is the one it is"],
    ], [52 * mm, 118 * mm]))
    s.append(Spacer(1, 8))
    s.append(Paragraph(
        "Generated by tools/build_pipeline_addendum.py. The first document's generator was lost; "
        "this one is in the repository so that cannot happen again.", SMALL))

    doc.build(s)
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    build()

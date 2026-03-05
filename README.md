# Selective Ironing

A PrusaSlicer post-processing script that enables precise ironing of graphics on a 3D print's top surface. This tool lets you create smooth, detailed surface designs by selectively applying ironing only where you want it.

## Features

- Automatically detects the top layer and applies selective ironing
- Converts designated surface details into ironing passes
- Handles retraction and travel moves intelligently
- Zero external dependencies (Python 3.6+ only)
- Prevents accidental double-processing

## Installation

1. Download the `selective-iron-postprocess.py` script
2. Note its full file path
3. In PrusaSlicer, go to **Print Settings** → **Output options** → **Post-processing scripts**
4. Paste the absolute path to the script

## Setup Instructions

### In PrusaSlicer:
1. Enable **Ironing** for the **Topmost surface only**
2. Set ironing **Flow rate** to **0%**

### In Your Model:
Design your part with a 1-layer-tall extrusion on the topmost surface representing the graphics you want ironed.

### Slicing:
Slice and print normally. Note that post-processing effects won't display in the slicer preview—the final result will appear on the printed part.

## How It Works

The script:
- Extracts all ironing move blocks from the top layer
- Automatically detects layer height and Z position
- Converts flat travel moves into proper retract/lift/travel/lower sequences
- Positions ironing moves at the optimal height for surface polishing
- Replaces the entire top layer with only the ironing passes

## Requirements

- Python 3.6 or later
- PrusaSlicer
- No external dependencies



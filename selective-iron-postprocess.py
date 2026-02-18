#!/usr/bin/env python3
"""
selective_iron.py — PrusaSlicer post-processing script

Converts the last sliced layer into a selective ironing layer.

The workflow:
  1. Model your print normally, with a one-layer-height raised silhouette
     on top representing the shape you want ironed.
  2. Enable ironing (topmost layer) in PrusaSlicer.
  3. Add this script under Print Settings → Output options → Post-processing scripts:
       python3 /path/to/selective_iron.py

The script will:
  - Find the last layer and detect its height automatically.
  - Extract all ;TYPE:Ironing blocks from that layer.
  - Drop every Z coordinate in those blocks by one layer height,
    so the ironing runs on the layer below (the actual top surface).
  - Replace the entire last layer with only the ironing passes.
  - Write the result back to the same file (PrusaSlicer expects in-place editing).

Requirements: Python 3.6+, no external dependencies.
"""

import sys
import re
import os
from typing import Optional, List


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_z(line: str) -> Optional[float]:
    """Return the Z value from a G1 Z... line, or None if not present."""
    m = re.search(r'G1\s+Z([0-9.]+)', line)
    return float(m.group(1)) if m else None


def replace_z(line: str, new_z: float) -> str:
    """Replace the Z value in a G1 Z... line."""
    return re.sub(r'(G1\s+Z)[0-9.]+', lambda _: f'G1 Z{new_z:.4f}'.rstrip('0').rstrip('.'), line)


def format_z(z: float) -> str:
    """Format a Z float the same compact way PrusaSlicer does (e.g. .7 not 0.7)."""
    s = f'{z:.4f}'.rstrip('0').rstrip('.')
    # PrusaSlicer omits the leading zero: 0.7 → .7
    if s.startswith('0.'):
        s = s[1:]
    elif s.startswith('-0.'):
        s = '-' + s[2:]
    return s


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process(filepath: str) -> None:
    with open(filepath, 'r') as f:
        lines = f.readlines()

    # ------------------------------------------------------------------
    # Step 1: Split into layers.
    # Each layer is a list of lines. Layer 0 = everything before the first
    # ;LAYER_CHANGE marker (startup / preamble).
    # ------------------------------------------------------------------
    layers = []
    current = []
    for line in lines:
        if line.strip() == ';LAYER_CHANGE' and current:
            layers.append(current)
            current = []
        current.append(line)
    if current:
        layers.append(current)

    if len(layers) < 2:
        print('[selective_iron] ERROR: Could not find any layer changes. Aborting.')
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 2: Identify the last layer and its Z height.
    # PrusaSlicer emits ;Z:<value> immediately after ;LAYER_CHANGE.
    # ------------------------------------------------------------------
    last_layer = layers[-1]

    last_layer_z = None
    for line in last_layer:
        m = re.match(r';Z:([0-9.]+)', line.strip())
        if m:
            last_layer_z = float(m.group(1))
            break

    if last_layer_z is None:
        print('[selective_iron] ERROR: Could not determine last layer Z. Aborting.')
        sys.exit(1)

    # Detect layer height from ;HEIGHT: comment in the last layer
    layer_height = None
    for line in last_layer:
        m = re.match(r';HEIGHT:([0-9.]+)', line.strip())
        if m:
            layer_height = float(m.group(1))
            break

    # Fallback: infer from previous layer's Z
    if layer_height is None and len(layers) >= 2:
        prev_layer = layers[-2]
        prev_z = None
        for line in prev_layer:
            m = re.match(r';Z:([0-9.]+)', line.strip())
            if m:
                prev_z = float(m.group(1))
                break
        if prev_z is not None:
            layer_height = round(last_layer_z - prev_z, 4)

    if layer_height is None:
        print('[selective_iron] ERROR: Could not determine layer height. Aborting.')
        sys.exit(1)

    target_z = round(last_layer_z - layer_height, 4)

    print(f'[selective_iron] Last layer Z: {last_layer_z} mm')
    print(f'[selective_iron] Layer height:  {layer_height} mm')
    print(f'[selective_iron] Ironing will run at Z: {target_z} mm')

    # ------------------------------------------------------------------
    # Step 3: Extract all ;TYPE:Ironing blocks from the last layer.
    #
    # A block starts at the ;TYPE:Ironing line and ends just before the
    # next ;TYPE: line (or end of layer). We include the travel moves
    # that PrusaSlicer emits between ironing regions (they're inside the
    # Ironing type span).
    # ------------------------------------------------------------------
    ironing_blocks = []   # list of lists-of-lines
    in_ironing = False
    current_block = []

    for line in last_layer:
        stripped = line.strip()

        if stripped == ';TYPE:Ironing':
            in_ironing = True
            current_block = [line]
            continue

        if stripped.startswith(';TYPE:') and in_ironing:
            # End of this ironing block
            ironing_blocks.append(current_block)
            current_block = []
            in_ironing = False
            continue

        if in_ironing:
            current_block.append(line)

    # Catch a block that runs to the end of the layer
    if in_ironing and current_block:
        ironing_blocks.append(current_block)

    if not ironing_blocks:
        print('[selective_iron] ERROR: No ;TYPE:Ironing sections found in the last layer.')
        print('                 Make sure ironing is enabled in PrusaSlicer.')
        sys.exit(1)

    print(f'[selective_iron] Found {len(ironing_blocks)} ironing block(s).')

    # ------------------------------------------------------------------
    # Step 4: Rewrite Z values in the ironing blocks.
    #
    # Every G1 Z move in the ironing blocks uses either:
    #   last_layer_z      → the working Z (nozzle touching surface)
    #   last_layer_z + x  → a travel lift above that layer
    #
    # We subtract layer_height from all of them so they now address the
    # layer below.
    # ------------------------------------------------------------------
    def shift_z_in_block(block: List[str]) -> List[str]:
        result = []
        for line in block:
            z_val = parse_z(line)
            if z_val is not None:
                new_z = round(z_val - layer_height, 4)
                line = replace_z(line, new_z)
            result.append(line)
        return result

    shifted_blocks = [shift_z_in_block(b) for b in ironing_blocks]

    # ------------------------------------------------------------------
    # Step 5: Build the replacement last layer.
    #
    # We keep:
    #   - The layer header up to and including ;AFTER_LAYER_CHANGE
    #     (with its Z updated to target_z)
    #   - All ironing blocks (Z-shifted)
    #   - The end-of-file footer (everything after the last layer's
    #     extrusion moves: fan off, park, etc.)
    #
    # The header lines we keep: ;LAYER_CHANGE, ;Z:, ;HEIGHT:,
    # ;BEFORE_LAYER_CHANGE block, the G1 Z move, ;AFTER_LAYER_CHANGE.
    # We stop collecting header lines once we hit the first ;TYPE: marker.
    # ------------------------------------------------------------------
    header_lines = []
    collecting_header = True

    for line in last_layer:
        stripped = line.strip()

        if collecting_header:
            if stripped.startswith(';TYPE:'):
                collecting_header = False
                # Don't include this TYPE line — ironing blocks have their own
                continue

            # Update Z references in header lines
            z_val = parse_z(line)
            if z_val is not None:
                line = replace_z(line, round(z_val - layer_height, 4))

            # Update ;Z: comment
            if stripped.startswith(';Z:'):
                line = f';Z:{format_z(target_z)}\n'

            header_lines.append(line)

    # Find the end-of-print footer: everything after the last extrusion
    # in the last layer. We detect it by finding the final M107/M104/park
    # sequence. The cleanest marker is ;TYPE:Custom near the end.
    footer_lines = []
    in_footer = False
    for line in last_layer:
        if line.strip() == ';TYPE:Custom' and not in_footer:
            # Check we're past the ironing (not the first ;TYPE:Custom
            # which appears in earlier layers)
            in_footer = True
        if in_footer:
            footer_lines.append(line)

    # Assemble the new last layer
    new_last_layer = header_lines[:]
    for block in shifted_blocks:
        new_last_layer.extend(block)
    if footer_lines:
        new_last_layer.extend(footer_lines)

    # ------------------------------------------------------------------
    # Step 6: Reassemble and write back.
    # ------------------------------------------------------------------
    layers[-1] = new_last_layer
    output_lines = []
    for layer in layers:
        output_lines.extend(layer)

    with open(filepath, 'w') as f:
        f.writelines(output_lines)

    print(f'[selective_iron] Done. Written back to: {filepath}')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 selective_iron.py <path_to_gcode>')
        sys.exit(1)

    gcode_path = sys.argv[1]

    if not os.path.isfile(gcode_path):
        print(f'[selective_iron] ERROR: File not found: {gcode_path}')
        sys.exit(1)

    process(gcode_path)

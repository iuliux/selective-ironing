#!/usr/bin/env python3
"""
selective_iron.py — PrusaSlicer post-processing script

Converts the last sliced layer into a selective ironing layer.

The workflow:
  1. Model your print normally, with a one-layer-height raised silhouette
     on top representing the shape you want ironed.
  2. Enable ironing (topmost layer) in PrusaSlicer.
  3. Add this script under Print Settings -> Output options -> Post-processing scripts:
       python3 /path/to/selective_iron.py

The script will:
  - Refuse to run on already-processed files (checks for ; selective_iron marker).
  - Find the last layer and detect its Z height and layer height automatically.
  - Read retraction settings from the G-code config footer.
  - Extract all ;TYPE:Ironing blocks from that layer (ironing moves only).
  - Generate a clean standardized entry travel for each block.
  - Lower all ironing Z moves to target_z (surface + IRON_Z_OFFSET).
  - Convert flat inter-line hops inside ironing passes into proper
    retract + lift + travel + lower + prime sequences.
  - Replace the entire last layer with only the ironing passes.
  - Write the result back to the same file (PrusaSlicer expects in-place editing).

Requirements: Python 3.6+, no external dependencies.
"""

import sys
import re
import os

# How far above the target surface the nozzle irons.
# PrusaSlicer uses 0.005 mm for its own ironing. Increase if you get raised
# edges, decrease if the ironing has no visible smoothing effect.
IRON_Z_OFFSET = 0.12

# Marker written into the output so we can detect double-processing.
PROCESSED_MARKER = '; selective_iron: processed'


def parse_z(line):
    """Return the Z value from a G1 Z... move, or None."""
    m = re.search(r'\bG1\b[^;]*\bZ([0-9.]+)', line)
    return float(m.group(1)) if m else None


def format_z(z):
    """Format Z compactly like PrusaSlicer does (.7 not 0.7)."""
    s = '{:.4f}'.format(z).rstrip('0').rstrip('.')
    if s.startswith('0.'):
        s = s[1:]
    elif s.startswith('-0.'):
        s = '-' + s[2:]
    return s


def parse_config_value(lines, key):
    """Extract a numeric value from the PrusaSlicer config footer."""
    pattern = re.compile(r';\s*' + re.escape(key) + r'\s*=\s*([0-9.]+)')
    for line in lines:
        m = pattern.match(line.strip())
        if m:
            return float(m.group(1))
    return None


def extract_xy(line):
    """Extract X and Y values from a G1 move, or return None."""
    xm = re.search(r'\bX([0-9.]+)', line)
    ym = re.search(r'\bY([0-9.]+)', line)
    if xm and ym:
        return xm.group(1), ym.group(1)
    return None


def process(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()

    # ------------------------------------------------------------------
    # Guard: refuse to run on already-processed files.
    # ------------------------------------------------------------------
    if any(PROCESSED_MARKER in line for line in lines):
        print('[selective_iron] ERROR: This file has already been processed. '
              'Run the script on the original exported G-code. Aborting.')
        sys.exit(1)

    # ------------------------------------------------------------------
    # Step 1: Split into layers.
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
    # Step 2: Detect last layer Z and layer height.
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

    layer_height = None
    for line in last_layer:
        m = re.match(r';HEIGHT:([0-9.]+)', line.strip())
        if m:
            layer_height = float(m.group(1))
            break

    if layer_height is None and len(layers) >= 2:
        for line in layers[-2]:
            m = re.match(r';Z:([0-9.]+)', line.strip())
            if m:
                layer_height = round(last_layer_z - float(m.group(1)), 4)
                break

    if layer_height is None:
        print('[selective_iron] ERROR: Could not determine layer height. Aborting.')
        sys.exit(1)

    surface_z = round(last_layer_z - layer_height, 4)
    target_z  = round(surface_z + IRON_Z_OFFSET, 4)

    print('[selective_iron] Last layer Z:  {} mm'.format(last_layer_z))
    print('[selective_iron] Layer height:   {} mm'.format(layer_height))
    print('[selective_iron] Surface Z:      {} mm'.format(surface_z))
    print('[selective_iron] Ironing Z:      {} mm (+{} mm offset)'.format(target_z, IRON_Z_OFFSET))

    # ------------------------------------------------------------------
    # Step 3: Read retraction settings from the config footer.
    # ------------------------------------------------------------------
    retract_length  = parse_config_value(lines, 'retract_length')  or 1.6
    retract_speed   = parse_config_value(lines, 'retract_speed')   or 25.0
    deretract_speed = parse_config_value(lines, 'deretract_speed') or retract_speed
    retract_lift    = parse_config_value(lines, 'retract_lift')    or 0.2

    lift_z = round(last_layer_z + retract_lift, 4)

    retract_feedrate   = int(retract_speed * 60)
    deretract_feedrate = int(deretract_speed * 60)

    print('[selective_iron] Retract:        {} mm @ {} mm/s'.format(retract_length, retract_speed))
    print('[selective_iron] Travel lift Z:  {} mm'.format(lift_z))

    # ------------------------------------------------------------------
    # Step 4: Extract ironing blocks (moves only, no entry travel).
    #
    # We collect only the lines between ;TYPE:Ironing and the next ;TYPE:
    # The entry travel is NOT captured here — we generate it ourselves in
    # Step 5 so it's consistent regardless of what PrusaSlicer emitted.
    # ------------------------------------------------------------------
    ironing_blocks = []   # list of raw move lines for each block
    in_ironing = False
    current_block = []

    for line in last_layer:
        stripped = line.strip()

        if stripped == ';TYPE:Ironing':
            in_ironing = True
            current_block = [line]
            continue

        if stripped.startswith(';TYPE:') and in_ironing:
            ironing_blocks.append(current_block)
            current_block = []
            in_ironing = False
            continue

        if in_ironing:
            current_block.append(line)

    if in_ironing and current_block:
        ironing_blocks.append(current_block)

    if not ironing_blocks:
        print('[selective_iron] ERROR: No ;TYPE:Ironing sections found in the last layer.')
        print('                 Make sure ironing is enabled in PrusaSlicer.')
        sys.exit(1)

    print('[selective_iron] Found {} ironing block(s).'.format(len(ironing_blocks)))

    # ------------------------------------------------------------------
    # Step 5: Transform each ironing block.
    #
    # For each block:
    #   a) Extract the XY start position from the first ironing move.
    #   b) Prepend a standardized entry: retract → lift → travel → lower → prime.
    #   c) Set all Z moves explicitly: working Z → target_z, lifts → lift_z.
    #   d) Wrap inter-line hops (F>=3000, no E, inside active ironing) with
    #      retract + lift + travel + lower + prime.
    # ------------------------------------------------------------------
    def get_first_ironing_xy(block):
        """Find XY of the first ironing move (first G1 with E after G1 F900)."""
        past_f900 = False
        for line in block:
            s = line.strip()
            if s == 'G1 F900' or s.startswith('G1 F900 '):
                past_f900 = True
                continue
            if past_f900 and re.search(r'\bE', s) and re.search(r'\bX', s):
                return extract_xy(s)
        return None

    def transform_block(block):
        result = []
        in_active_ironing = False

        # Generate standardized entry travel to the block's start position
        xy = get_first_ironing_xy(block)
        if xy:
            x, y = xy
            result.append('G1 E-{} F{} ; retract\n'.format(retract_length, retract_feedrate))
            result.append('G1 Z{} F720 ; lift\n'.format(format_z(lift_z)))
            result.append('G1 X{} Y{} F10800 ; travel to ironing start\n'.format(x, y))
            result.append('G1 Z{} F720 ; lower\n'.format(format_z(target_z)))
            result.append('G1 E{} F{} ; prime\n'.format(retract_length, deretract_feedrate))

        for line in block:
            stripped = line.strip()

            # Track active ironing region
            if stripped == 'G1 F900' or stripped.startswith('G1 F900 '):
                in_active_ironing = True
                result.append(line)
                continue

            if stripped in (';WIPE_START', ';WIPE_END'):
                in_active_ironing = False
                result.append(line)
                continue

            # Set Z explicitly
            z_val = parse_z(line)
            if z_val is not None:
                new_z = target_z if round(z_val, 4) <= round(last_layer_z, 4) else lift_z
                line = re.sub(r'(G1\s+Z)[0-9.]+', 'G1 Z' + format_z(new_z), line)
                result.append(line)
                continue

            # Inter-line hop inside active ironing — wrap it
            if in_active_ironing and stripped.startswith('G1') and not re.search(r'\bE', stripped):
                f_match = re.search(r'\bF([0-9.]+)', stripped)
                if f_match and float(f_match.group(1)) >= 3000:
                    result.append('G1 E-{} F{} ; retract\n'.format(retract_length, retract_feedrate))
                    result.append('G1 Z{} F720 ; lift\n'.format(format_z(lift_z)))
                    result.append(line)
                    result.append('G1 Z{} F720 ; lower\n'.format(format_z(target_z)))
                    result.append('G1 E{} F{} ; prime\n'.format(retract_length, deretract_feedrate))
                    continue

            result.append(line)

        return result

    transformed_blocks = [transform_block(block) for block in ironing_blocks]

    # ------------------------------------------------------------------
    # Step 6: Build the replacement last layer.
    #
    # Header = everything up to and including ;AFTER_LAYER_CHANGE,
    # plus any COLOR_CHANGE block that follows (up to the first ;TYPE:).
    # ------------------------------------------------------------------
    header_lines = []
    past_after_layer = False
    for line in last_layer:
        stripped = line.strip()

        if past_after_layer and stripped.startswith(';TYPE:'):
            break

        if stripped.startswith(';Z:'):
            line = ';Z:{}\n'.format(format_z(target_z))

        z_val = parse_z(line)
        if z_val is not None:
            new_z = target_z if round(z_val, 4) <= round(last_layer_z, 4) else lift_z
            line = re.sub(r'(G1\s+Z)[0-9.]+', 'G1 Z' + format_z(new_z), line)

        header_lines.append(line)

        if stripped == ';AFTER_LAYER_CHANGE':
            past_after_layer = True

    footer_lines = []
    in_footer = False
    for line in last_layer:
        if not in_footer and line.strip() == ';TYPE:Custom':
            in_footer = True
        if in_footer:
            footer_lines.append(line)

    new_last_layer = header_lines[:]
    for block in transformed_blocks:
        new_last_layer.extend(block)
    if footer_lines:
        new_last_layer.extend(footer_lines)

    # ------------------------------------------------------------------
    # Step 7: Reassemble and write back in-place.
    # ------------------------------------------------------------------
    layers[-1] = new_last_layer
    output_lines = []
    for layer in layers:
        output_lines.extend(layer)

    marker_line = PROCESSED_MARKER + '\n'
    for i, line in enumerate(output_lines):
        if '; prusaslicer_config = begin' in line:
            output_lines.insert(i, marker_line)
            break
    else:
        output_lines.append(marker_line)

    with open(filepath, 'w') as f:
        f.writelines(output_lines)

    print('[selective_iron] Done. Written back to: {}'.format(filepath))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 selective_iron.py <path_to_gcode>')
        sys.exit(1)
    gcode_path = sys.argv[1]
    if not os.path.isfile(gcode_path):
        print('[selective_iron] ERROR: File not found: {}'.format(gcode_path))
        sys.exit(1)
    process(gcode_path)

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
  - Extract all ;TYPE:Ironing blocks from that layer, including the
    pre-block positioning travel (retract/lift/travel/lower/prime).
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
IRON_Z_OFFSET = 0.05

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

    # The surface we want to iron is one layer below.
    surface_z = round(last_layer_z - layer_height, 4)
    # The nozzle irons slightly above the surface.
    target_z  = round(surface_z + IRON_Z_OFFSET, 4)
    # Travel lifts go above last_layer_z to clear the silhouette layer.
    lift_z    = round(last_layer_z + 0.2, 4)   # same margin as retract_lift

    print('[selective_iron] Last layer Z:  {} mm'.format(last_layer_z))
    print('[selective_iron] Layer height:   {} mm'.format(layer_height))
    print('[selective_iron] Surface Z:      {} mm'.format(surface_z))
    print('[selective_iron] Ironing Z:      {} mm (+{} mm offset)'.format(target_z, IRON_Z_OFFSET))
    print('[selective_iron] Travel lift Z:  {} mm'.format(lift_z))

    # ------------------------------------------------------------------
    # Step 3: Read retraction settings from the config footer.
    # ------------------------------------------------------------------
    retract_length  = parse_config_value(lines, 'retract_length')  or 1.6
    retract_speed   = parse_config_value(lines, 'retract_speed')   or 25.0
    deretract_speed = parse_config_value(lines, 'deretract_speed') or retract_speed
    retract_lift    = parse_config_value(lines, 'retract_lift')    or 0.2

    # Recalculate lift_z using the actual retract_lift from the config
    lift_z = round(last_layer_z + retract_lift, 4)

    retract_feedrate   = int(retract_speed * 60)
    deretract_feedrate = int(deretract_speed * 60)

    print('[selective_iron] Retract:        {} mm @ {} mm/s'.format(retract_length, retract_speed))
    print('[selective_iron] Travel lift Z:  {} mm'.format(lift_z))

    # ------------------------------------------------------------------
    # Step 4: Extract ironing blocks with their entry travel.
    #
    # PrusaSlicer emits this before each ;TYPE:Ironing block:
    #   [wipe end]
    #   G1 Z<lift>               (lift)
    #   G1 X.. Y.. F10800        (travel to region start)
    #   G1 Z<layer_z>            (lower to layer Z)
    #   G1 E1.6 F1500            (prime)
    #   ;TYPE:Ironing
    #
    # We look back from ;TYPE:Ironing to capture the entry, stopping at
    # ;WIPE_END which is the definitive boundary before it.
    # ------------------------------------------------------------------
    def find_entry_start(layer_lines, type_ironing_idx):
        i = type_ironing_idx - 1
        while i >= 0:
            s = layer_lines[i].strip()
            if (s == ';WIPE_END' or
                    s.startswith(';TYPE:') or
                    s.startswith(';AFTER_LAYER_CHANGE')):
                return i + 1
            i -= 1
        return 0

    ironing_blocks = []
    in_ironing = False
    current_block = []
    block_entry_start = None

    for idx, line in enumerate(last_layer):
        stripped = line.strip()

        if stripped == ';TYPE:Ironing':
            entry_start = find_entry_start(last_layer, idx)
            in_ironing = True
            current_block = list(last_layer[entry_start:idx]) + [line]
            block_entry_start = entry_start
            continue

        if stripped.startswith(';TYPE:') and in_ironing:
            ironing_blocks.append((block_entry_start, current_block))
            current_block = []
            in_ironing = False
            block_entry_start = None
            continue

        if in_ironing:
            current_block.append(line)

    if in_ironing and current_block:
        ironing_blocks.append((block_entry_start, current_block))

    if not ironing_blocks:
        print('[selective_iron] ERROR: No ;TYPE:Ironing sections found in the last layer.')
        print('                 Make sure ironing is enabled in PrusaSlicer.')
        sys.exit(1)

    print('[selective_iron] Found {} ironing block(s).'.format(len(ironing_blocks)))

    # ------------------------------------------------------------------
    # Step 5: Transform each ironing block.
    #
    # Z strategy — instead of shifting relative to last_layer_z (fragile
    # if the file has been partially processed), we set Z values explicitly:
    #   - Any Z move at or below last_layer_z  →  target_z  (ironing contact)
    #   - Any Z move above last_layer_z        →  lift_z    (travel clearance)
    #
    # Inter-line hops inside active ironing (after the first G1 F900,
    # reset by wipe markers) are wrapped with retract/lift/travel/lower/prime.
    # ------------------------------------------------------------------
    def transform_block(block):
        result = []
        in_active_ironing = False

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

            # Z: set explicitly to target_z or lift_z
            z_val = parse_z(line)
            if z_val is not None:
                if round(z_val, 4) <= round(last_layer_z, 4):
                    new_z = target_z
                else:
                    new_z = lift_z
                new_z_str = format_z(new_z)
                line = re.sub(r'(G1\s+Z)[0-9.]+', 'G1 Z' + new_z_str, line)
                result.append(line)
                continue

            # Inter-line hop inside active ironing — wrap with retract/lift/travel/lower/prime
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

    transformed_blocks = [transform_block(block) for _, block in ironing_blocks]

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

        # Once past AFTER_LAYER_CHANGE, stop at the first TYPE marker
        if past_after_layer and stripped.startswith(';TYPE:'):
            break

        # Update ;Z: comment to reflect new ironing Z
        if stripped.startswith(';Z:'):
            line = ';Z:{}\n'.format(format_z(target_z))

        # Update any G1 Z moves in the header
        z_val = parse_z(line)
        if z_val is not None:
            if round(z_val, 4) <= round(last_layer_z, 4):
                line = re.sub(r'(G1\s+Z)[0-9.]+', 'G1 Z' + format_z(target_z), line)
            else:
                line = re.sub(r'(G1\s+Z)[0-9.]+', 'G1 Z' + format_z(lift_z), line)

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
    # Stamp the processed marker into the footer so double-runs are caught.
    # ------------------------------------------------------------------
    layers[-1] = new_last_layer
    output_lines = []
    for layer in layers:
        output_lines.extend(layer)

    # Insert marker just before the prusaslicer_config block (or at end)
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

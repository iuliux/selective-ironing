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
  - Find the last layer and detect its Z height and layer height automatically.
  - Read retraction settings from the G-code config footer.
  - Extract all ;TYPE:Ironing blocks from that layer, including the
    pre-block positioning travel (retract/lift/travel/lower/prime).
  - Lower all ironing Z moves by one layer height so ironing runs on the
    layer below (the actual top surface).
  - Convert the flat F10800 inter-line hops *inside* active ironing passes
    into proper retract + lift + travel + lower + prime sequences.
  - Replace the entire last layer with only the ironing passes.
  - Write the result back to the same file (PrusaSlicer expects in-place editing).

Requirements: Python 3.6+, no external dependencies.
"""

import sys
import re
import os


def parse_z(line):
    """Return the Z value from a G1 Z... move, or None."""
    m = re.search(r'\bG1\b[^;]*\bZ([0-9.]+)', line)
    return float(m.group(1)) if m else None


def replace_z(line, new_z):
    """Replace the Z value in a G1 Z... line."""
    formatted = '{:.4f}'.format(new_z).rstrip('0').rstrip('.')
    return re.sub(r'(G1\s+Z)[0-9.]+', 'G1 Z' + formatted, line)


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

    target_z = round(last_layer_z - layer_height, 4)

    print('[selective_iron] Last layer Z:  {} mm'.format(last_layer_z))
    print('[selective_iron] Layer height:   {} mm'.format(layer_height))
    print('[selective_iron] Ironing Z:      {} mm'.format(target_z))

    # ------------------------------------------------------------------
    # Step 3: Read retraction settings from the config footer.
    # ------------------------------------------------------------------
    retract_length  = parse_config_value(lines, 'retract_length')  or 1.6
    retract_speed   = parse_config_value(lines, 'retract_speed')   or 25.0
    deretract_speed = parse_config_value(lines, 'deretract_speed') or retract_speed
    retract_lift    = parse_config_value(lines, 'retract_lift')    or 0.2

    retract_feedrate   = int(retract_speed * 60)
    deretract_feedrate = int(deretract_speed * 60)
    lift_z = round(target_z + retract_lift, 4)

    print('[selective_iron] Retract:        {} mm @ {} mm/s'.format(retract_length, retract_speed))
    print('[selective_iron] Lift Z:         {} mm'.format(lift_z))

    # ------------------------------------------------------------------
    # Step 4: Extract ironing blocks with their entry travel.
    #
    # PrusaSlicer emits this before each ;TYPE:Ironing block:
    #   G1 Z.9 F720              (lift after wipe)
    #   G1 X.. Y.. F10800        (travel to region start)
    #   G1 Z.7 F720              (lower to ironing Z)
    #   G1 E1.6 F1500            (prime)
    #   ;TYPE:Ironing
    #
    # We look back from ;TYPE:Ironing to find where this entry starts,
    # stopping at ;WIPE_END (which marks the definitive boundary between
    # the prior content and the entry sequence).
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

    ironing_blocks = []   # list of (entry_start_idx, block_lines)
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
    # a) Z shift: lower all working-Z moves (at or below last_layer_z)
    #    by one layer height. Lifts above last_layer_z stay unchanged.
    #
    # b) Inter-line hops: only wrap with retract/lift/travel/lower/prime
    #    once we are inside active ironing (after the first G1 F900).
    #    Before that point (the entry travel) hops are intentional
    #    positioning moves and must not be wrapped.
    # ------------------------------------------------------------------
    def transform_block(block):
        result = []
        in_active_ironing = False

        for line in block:
            stripped = line.strip()

            # Mark the start of active ironing passes
            if stripped == 'G1 F900' or stripped.startswith('G1 F900 '):
                in_active_ironing = True
                result.append(line)
                continue

            # Wipe markers mean we are outside active ironing
            if stripped in (';WIPE_START', ';WIPE_END'):
                in_active_ironing = False
                result.append(line)
                continue

            # Z shift
            z_val = parse_z(line)
            if z_val is not None:
                if round(z_val, 4) <= round(last_layer_z, 4):
                    line = replace_z(line, round(z_val - layer_height, 4))
                # lifts above last_layer_z stay as-is
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

    transformed_blocks = [transform_block(block) for _, block in ironing_blocks]

    # ------------------------------------------------------------------
    # Step 6: Build the replacement last layer.
    #
    # Header = everything up to the entry of the first ironing block,
    # with Z values lowered. Then all transformed blocks. Then footer.
    # ------------------------------------------------------------------
    first_entry_start = ironing_blocks[0][0]

    header_lines = []
    for line in last_layer[:first_entry_start]:
        stripped = line.strip()
        z_val = parse_z(line)
        if z_val is not None:
            line = replace_z(line, round(z_val - layer_height, 4))
        if stripped.startswith(';Z:'):
            line = ';Z:{}\n'.format(format_z(target_z))
        header_lines.append(line)

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

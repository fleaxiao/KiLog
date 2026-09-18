"""Read-only reproduction for ref_065 (rectangular outline, SMD pads, segments)."""
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kipy.board_types import FootprintInstance, Pad, PadType, Net, Track, BoardLayer, Via
from kipy.geometry import Vector2, Angle
from kilog.kicad_adapter import KiCadBoardAdapter
from kilog.board_outline import point_inside_board


def read_board(path):
    tokens = iter(re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', Path(path).read_text(encoding="utf-8")))
    def read():
        result = []
        for token in tokens:
            if token == ')':
                return result
            result.append(read() if token == '(' else json.loads(token) if token.startswith('"') else token)
        return result
    return read()[0]


def field(node, name, default=()):
    return next((v[1:] for v in node if isinstance(v, list) and v[0] == name), default)


def nm(value):
    return round(float(value) * 1_000_000)


def vector(values):
    return Vector2.from_xy(*map(nm, values[:2]))


def load_geometry(path):
    nodes = read_board(path)
    footprints, tracks, vias, loops = [], [], [], []
    for node in nodes:
        if not isinstance(node, list):
            continue
        kind = node[0]
        if kind == 'footprint':
            fp = FootprintInstance()
            at = field(node, 'at')
            fp.position = vector(at)
            angle = math.radians(float(at[2]) if len(at) > 2 else 0)
            fp.layer = BoardLayer.BL_F_Cu if field(node, 'layer')[0] == 'F.Cu' else BoardLayer.BL_B_Cu
            fp.reference_field.text.value = next(v[2] for v in node if isinstance(v, list) and v[:2] == ['property', 'Reference'])
            for data in node:
                if not isinstance(data, list) or data[0] != 'pad':
                    continue
                pad = Pad()
                pad.number = data[1]
                pad.pad_type = PadType.PT_SMD if data[2] == 'smd' else PadType.PT_PTH
                pad.net = Net(name=field(data, 'net', [''])[-1])
                at = field(data, 'at')
                x, y = map(nm, at[:2])
                pad.position = Vector2.from_xy(
                    fp.position.x + round(x * math.cos(angle) + y * math.sin(angle)),
                    fp.position.y + round(-x * math.sin(angle) + y * math.cos(angle)))
                pad.padstack.angle = Angle.from_degrees(-float(at[2]) if len(at) > 2 else 0)
                pad.padstack.copper_layers[0].size = vector(field(data, 'size'))
                pad.padstack.layers = [
                    BoardLayer.Value('BL_' + layer.replace('.', '_'))
                    for layer in field(data, 'layers') if not layer.startswith('*')
                ]
                fp.definition.add_item(pad)
            footprints.append(fp)
        elif kind == 'segment':
            track = Track()
            track.start, track.end = vector(field(node, 'start')), vector(field(node, 'end'))
            track.net = Net(name=field(node, 'net')[-1])
            track.width = nm(field(node, 'width')[0])
            track.layer = BoardLayer.BL_F_Cu if field(node, 'layer')[0] == 'F.Cu' else BoardLayer.BL_B_Cu
            tracks.append(track)
        elif kind == 'via':
            via = Via()
            via.position = vector(field(node, 'at'))
            via.diameter = nm(field(node, 'size')[0])
            via.net = Net(name=field(node, 'net')[-1])
            vias.append(via)
        elif kind == 'gr_rect' and field(node, 'layer') == ['Edge.Cuts']:
            x1, y1 = map(nm, field(node, 'start'))
            x2, y2 = map(nm, field(node, 'end'))
            loops.append([(x1, y1), (x2, y1), (x2, y2), (x1, y2)])
        elif kind == 'arc' or kind.startswith('gr_') and field(node, 'layer') == ['Edge.Cuts']:
            raise ValueError(f'Unsupported geometry: {kind}')
    footprints = [fp for fp in footprints if point_inside_board((fp.position.x, fp.position.y), loops)]
    return footprints, tracks, vias, loops


if __name__ == '__main__':
    footprints, tracks, vias, loops = load_geometry(sys.argv[1])
    adapter = KiCadBoardAdapter(None, None)
    obstacles = adapter._fanout_pad_obstacles(footprints)
    for fp in footprints:
        if fp.reference_field.text.value not in ('T1', 'U1'):
            continue
        width = max((adapter._fanout_track_width(p, tracks, 0) for p in fp.definition.pads), default=0) or 400_000
        diameter = max(300_000, round(500_000 * width / 400_000))
        width = max(300_000, width)
        for pad in fp.definition.pads:
            if pad.net.name != 'GND':
                continue
            args = [pad, fp, fp.layer, loops, obstacles, [(v.position.x, v.position.y, adapter._via_radius(v)) for v in vias], tracks, 65_000_000, width, diameter]
            print(fp.reference_field.text.value, pad.number, 'pos', pad.position, 'width/via', width, diameter,
                  'result', adapter._find_fanout_position(*args))
            for index, name in ((4, 'pads'), (5, 'vias'), (6, 'tracks')):
                test = args.copy()
                test[index] = []
                print('without', name, adapter._find_fanout_position(*test))

# -*- coding: utf-8 -*-
#
"""
I/O for Gmsh's msh format, cf.
<http://gmsh.info//doc/texinfo/gmsh.html#File-formats>.
"""
from ctypes import c_ulong, c_double, c_int
import logging
import struct

import numpy

from .common import (
    _read_physical_names,
    _read_periodic,
    _gmsh_to_meshio_type,
    _meshio_to_gmsh_type,
    _write_physical_names,
    _write_periodic,
    _read_data,
    _write_data,
)
from ..mesh import Mesh
from ..common import num_nodes_per_cell, cell_data_from_raw, raw_from_cell_data


def read_buffer(f, is_ascii, int_size, data_size):
    # The format is specified at
    # <http://gmsh.info//doc/texinfo/gmsh.html#MSH-ASCII-file-format>.

    # Initialize the optional data fields
    points = []
    cells = {}
    field_data = {}
    cell_data_raw = {}
    cell_tags = {}
    point_data = {}
    periodic = None
    while True:
        line = f.readline().decode("utf-8")
        if not line:
            # EOF
            break
        assert line[0] == "$"
        environ = line[1:].strip()

        if environ == "PhysicalNames":
            _read_physical_names(f, field_data)
        elif environ == "Nodes":
            points, point_tags = _read_nodes(f, is_ascii, int_size, data_size)
        elif environ == "Elements":
            cells = _read_cells(f, point_tags, int_size, is_ascii)
        elif environ == "Periodic":
            periodic = _read_periodic(f)
        elif environ == "Entities":
            physical_tags = _read_entities(f, is_ascii, int_size, data_size)  # noqa F841
        elif environ == "NodeData":
            _read_data(f, "NodeData", point_data, int_size, data_size, is_ascii)
        elif environ == "ElementData":
            _read_data(f, "ElementData", cell_data_raw, int_size, data_size, is_ascii)
        else:
            # From
            # <http://gmsh.info//doc/texinfo/gmsh.html#MSH-file-format-_0028version-4_0029>:
            # ```
            # Any section with an unrecognized header is simply ignored: you can thus
            # add comments in a .msh file by putting them e.g. inside a
            # $Comments/$EndComments section.
            # ```
            # skip environment
            while line != "$End" + environ:
                line = f.readline().decode("utf-8").strip()

    cell_data = cell_data_from_raw(cells, cell_data_raw)

    # merge cell_tags into cell_data
    for key, tag_dict in cell_tags.items():
        if key not in cell_data:
            cell_data[key] = {}
        for name, item_list in tag_dict.items():
            assert name not in cell_data[key]
            cell_data[key][name] = item_list

    return Mesh(
        points,
        cells,
        point_data=point_data,
        cell_data=cell_data,
        field_data=field_data,
        gmsh_periodic=periodic,
    )


def _read_entities(f, is_ascii, int_size, data_size):
    physical_tags = ([], [], [], [])  # dims 0, 1, 2, 3
    if is_ascii:
        # More or less skip over them for now
        line = f.readline().decode("utf-8").strip()
        while line != "$EndEntities":
            line = f.readline().decode("utf-8").strip()
    else:
        num_points, num_curves, num_surfaces, num_volumes = numpy.fromfile(
            f, count=4, dtype=c_ulong
        )

        for _ in range(num_points):
            # tag
            numpy.fromfile(f, count=1, dtype=c_int)
            # box
            numpy.fromfile(f, count=6, dtype=c_double)
            # num_physicals
            num_physicals = numpy.fromfile(f, count=1, dtype=c_ulong)
            # physical_tags
            physical_tags[0].append(
                numpy.fromfile(f, count=int(num_physicals), dtype=c_int)
            )

        for _ in range(num_curves):
            # tag
            numpy.fromfile(f, count=1, dtype=c_int)
            # box
            numpy.fromfile(f, count=6, dtype=c_double)
            # num_physicals
            num_physicals = numpy.fromfile(f, count=1, dtype=c_ulong)
            # physical_tags
            physical_tags[1].append(
                numpy.fromfile(f, count=int(num_physicals), dtype=c_int)
            )
            # num_brep_vert
            num_brep_vert = numpy.fromfile(f, count=1, dtype=c_ulong)
            # tag_brep_vert
            numpy.fromfile(f, count=int(num_brep_vert), dtype=c_int)

        for _ in range(num_surfaces):
            # tag
            numpy.fromfile(f, count=1, dtype=c_int)
            # box
            numpy.fromfile(f, count=6, dtype=c_double)
            # num_physicals
            num_physicals = numpy.fromfile(f, count=1, dtype=c_ulong)
            # physical_tags
            physical_tags[2].append(
                numpy.fromfile(f, count=int(num_physicals), dtype=c_int)
            )
            # num_brep_curve
            num_brep_curve = numpy.fromfile(f, count=1, dtype=c_ulong)
            # tag_brep_curve
            numpy.fromfile(f, count=int(num_brep_curve), dtype=c_int)

        for _ in range(num_volumes):
            # tag
            numpy.fromfile(f, count=1, dtype=c_int)
            # box
            numpy.fromfile(f, count=6, dtype=c_double)
            # num_physicals
            num_physicals = numpy.fromfile(f, count=1, dtype=c_ulong)
            # physical_tags
            physical_tags[3].append(
                numpy.fromfile(f, count=int(num_physicals), dtype=c_int)
            )
            # num_brep_surfaces
            num_brep_surfaces = numpy.fromfile(f, count=1, dtype=c_ulong)
            # tag_brep_surfaces
            numpy.fromfile(f, count=int(num_brep_surfaces), dtype=c_int)

        line = f.readline().decode("utf-8")
        assert line == "\n"
        line = f.readline().decode("utf-8").strip()
        assert line == "$EndEntities"
    return physical_tags


def _read_nodes(f, is_ascii, int_size, data_size):
    if is_ascii:
        # first line: numEntityBlocks(unsigned long) numNodes(unsigned long)
        line = f.readline().decode("utf-8")
        num_entity_blocks, total_num_nodes = [int(k) for k in line.split()]

        points = numpy.empty((total_num_nodes, 3), dtype=float)
        tags = numpy.empty(total_num_nodes, dtype=int)

        idx = 0
        for k in range(num_entity_blocks):
            # first line in the entity block:
            # tagEntity(int) dimEntity(int) typeNode(int) numNodes(unsigned long)
            line = f.readline().decode("utf-8")
            tag_entity, dim_entity, type_node, num_nodes = map(int, line.split())
            for i in range(num_nodes):
                # tag(int) x(double) y(double) z(double)
                line = f.readline().decode("utf-8")
                tag, x, y, z = line.split()
                points[idx] = [float(x), float(y), float(z)]
                tags[idx] = tag
                idx += 1
    else:
        # numEntityBlocks(unsigned long) numNodes(unsigned long)
        num_entity_blocks, _ = numpy.fromfile(f, count=2, dtype=c_ulong)

        points = []
        tags = []
        for _ in range(num_entity_blocks):
            # tagEntity(int) dimEntity(int) typeNode(int) numNodes(unsigned long)
            numpy.fromfile(f, count=3, dtype=c_int)
            num_nodes = numpy.fromfile(f, count=1, dtype=c_ulong)
            dtype = [("tag", c_int), ("x", c_double, (3,))]
            data = numpy.fromfile(f, count=int(num_nodes), dtype=dtype)
            tags.append(data["tag"])
            points.append(data["x"])

        tags = numpy.concatenate(tags)
        points = numpy.concatenate(points)

        line = f.readline().decode("utf-8")
        assert line == "\n"

    line = f.readline().decode("utf-8")
    assert line.strip() == "$EndNodes"

    return points, tags


def _read_cells(f, point_tags, int_size, is_ascii):
    if is_ascii:
        # numEntityBlocks(unsigned long) numElements(unsigned long)
        line = f.readline().decode("utf-8")
        num_entity_blocks, total_num_elements = [int(k) for k in line.split()]

        data = []
        for k in range(num_entity_blocks):
            line = f.readline().decode("utf-8")
            # tagEntity(int) dimEntity(int) typeEle(int) numElements(unsigned long)
            tag_entity, dim_entity, type_ele, num_elements = [
                int(k) for k in line.split()
            ]
            tpe = _gmsh_to_meshio_type[type_ele]
            num_nodes_per_ele = num_nodes_per_cell[tpe]
            d = numpy.empty((num_elements, num_nodes_per_ele + 1), dtype=int)
            idx = 0
            for i in range(num_elements):
                # tag(int) numVert(int)[...]
                line = f.readline().decode("utf-8")
                items = line.split()
                d[idx] = [int(item) for item in items]
                idx += 1
            assert idx == num_elements
            data.append((tpe, d))

        line = f.readline().decode("utf-8")
        assert line.strip() == "$EndElements"
    else:
        # numEntityBlocks(unsigned long) numElements(unsigned long)
        num_entity_blocks, _ = numpy.fromfile(f, count=2, dtype=c_ulong)

        data = []
        for k in range(num_entity_blocks):
            # tagEntity(int) dimEntity(int) typeEle(int) numEle(unsigned long)
            _, _, type_ele = numpy.fromfile(f, count=3, dtype=c_int)

            tpe = _gmsh_to_meshio_type[type_ele]
            num_nodes_per_ele = num_nodes_per_cell[tpe]

            num_ele = numpy.fromfile(f, count=1, dtype=c_ulong)

            d = numpy.fromfile(
                f, count=int(num_ele * (num_nodes_per_ele + 1)), dtype=c_int
            ).reshape(int(num_ele), -1)

            data.append((tpe, d))

        line = f.readline().decode("utf-8")
        assert line == "\n"
        line = f.readline().decode("utf-8")
        assert line.strip() == "$EndElements"

    # The msh4 elements array refers to the nodes by their tag, not the index. All other
    # mesh formats use the index, which is far more efficient, too. Hence,
    # unfortunately, we have to do a fairly expensive conversion here.
    m = numpy.max(point_tags + 1)
    itags = -numpy.ones(m, dtype=int)
    for k, tag in enumerate(point_tags):
        itags[tag] = k
    # Note that the first column in the data array is the element tag; discard it.
    data = [(tpe, itags[d[:, 1:]]) for tpe, d in data]

    cells = {}
    for item in data:
        key, values = item
        if key in cells:
            cells[key] = numpy.concatenate([cells[key], values])
        else:
            cells[key] = values

    # Gmsh cells are mostly ordered like VTK, with a few exceptions:
    if "tetra10" in cells:
        cells["tetra10"] = cells["tetra10"][:, [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]]
    if "hexahedron20" in cells:
        cells["hexahedron20"] = cells["hexahedron20"][
            :, [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 16, 9, 17, 10, 18, 19, 12, 15, 13, 14]
        ]

    return cells


def write(filename, mesh, write_binary=True):
    """Writes msh files, cf.
    <http://gmsh.info//doc/texinfo/gmsh.html#MSH-ASCII-file-format>.
    """
    if write_binary:
        for key, value in mesh.cells.items():
            if value.dtype != c_int:
                logging.warning(
                    "Binary Gmsh needs c_int (typically numpy.int32) integers "
                    "(got %s). Converting.",
                    value.dtype,
                )
                mesh.cells[key] = numpy.array(value, dtype=c_int)

    # Gmsh cells are mostly ordered like VTK, with a few exceptions:
    cells = mesh.cells.copy()
    if "tetra10" in cells:
        cells["tetra10"] = cells["tetra10"][:, [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]]
    if "hexahedron20" in cells:
        cells["hexahedron20"] = cells["hexahedron20"][
            :, [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13, 9, 16, 18, 19, 17, 10, 12, 14, 15]
        ]

    with open(filename, "wb") as fh:
        mode_idx = 1 if write_binary else 0
        size_of_double = 8
        fh.write(
            ("$MeshFormat\n4 {} {}\n".format(mode_idx, size_of_double)).encode("utf-8")
        )
        if write_binary:
            fh.write(struct.pack("i", 1))
            fh.write("\n".encode("utf-8"))
        fh.write("$EndMeshFormat\n".encode("utf-8"))

        if mesh.field_data:
            _write_physical_names(fh, mesh.field_data)

        _write_nodes(fh, mesh.points, write_binary)
        _write_elements(fh, cells, write_binary)
        if mesh.gmsh_periodic is not None:
            _write_periodic(fh, mesh.gmsh_periodic)
        for name, dat in mesh.point_data.items():
            _write_data(fh, "NodeData", name, dat, write_binary)
        cell_data_raw = raw_from_cell_data(mesh.cell_data)
        for name, dat in cell_data_raw.items():
            _write_data(fh, "ElementData", name, dat, write_binary)

    return


def _write_nodes(fh, points, write_binary):
    fh.write("$Nodes\n".encode("utf-8"))

    # TODO not sure what dimEntity is supposed to say
    dim_entity = 0
    type_node = 0

    if write_binary:
        # write all points as one big block
        # numEntityBlocks(unsigned long) numNodes(unsigned long)
        # tagEntity(int) dimEntity(int) typeNode(int) numNodes(unsigned long)
        # tag(int) x(double) y(double) z(double)
        fh.write(numpy.array([1, points.shape[0]], dtype=c_ulong).tostring())
        fh.write(numpy.array([1, dim_entity, type_node], dtype=c_int).tostring())
        fh.write(numpy.array([points.shape[0]], dtype=c_ulong).tostring())
        dtype = [("index", c_int), ("x", c_double, (3,))]
        tmp = numpy.empty(len(points), dtype=dtype)
        tmp["index"] = 1 + numpy.arange(len(points))
        tmp["x"] = points
        fh.write(tmp.tostring())
        fh.write("\n".encode("utf-8"))
    else:
        # write all points as one big block
        # numEntityBlocks(unsigned long) numNodes(unsigned long)
        fh.write("{} {}\n".format(1, len(points)).encode("utf-8"))

        # tagEntity(int) dimEntity(int) typeNode(int) numNodes(unsigned long)
        fh.write(
            "{} {} {} {}\n".format(1, dim_entity, type_node, len(points)).encode(
                "utf-8"
            )
        )

        for k, x in enumerate(points):
            # tag(int) x(double) y(double) z(double)
            fh.write(
                "{} {!r} {!r} {!r}\n".format(k + 1, x[0], x[1], x[2]).encode("utf-8")
            )

    fh.write("$EndNodes\n".encode("utf-8"))
    return


def _write_elements(fh, cells, write_binary):
    # TODO respect write_binary
    # write elements
    fh.write("$Elements\n".encode("utf-8"))

    if write_binary:
        total_num_cells = sum([data.shape[0] for _, data in cells.items()])
        fh.write(numpy.array([len(cells), total_num_cells], dtype=c_ulong).tostring())

        consecutive_index = 0
        for cell_type, node_idcs in cells.items():
            # tagEntity(int) dimEntity(int) typeEle(int) numElements(unsigned long)
            fh.write(
                numpy.array(
                    [
                        1,
                        _geometric_dimension[cell_type],
                        _meshio_to_gmsh_type[cell_type],
                    ],
                    dtype=c_int,
                ).tostring()
            )
            fh.write(numpy.array([node_idcs.shape[0]], dtype=c_ulong).tostring())

            assert node_idcs.dtype == c_int
            data = numpy.column_stack(
                [
                    numpy.arange(
                        consecutive_index,
                        consecutive_index + len(node_idcs),
                        dtype=c_int,
                    ),
                    # increment indices by one to conform with gmsh standard
                    node_idcs + 1,
                ]
            )
            fh.write(data.tostring())
            consecutive_index += len(node_idcs)

        fh.write("\n".encode("utf-8"))
    else:
        # count all cells
        total_num_cells = sum([data.shape[0] for _, data in cells.items()])
        fh.write("{} {}\n".format(len(cells), total_num_cells).encode("utf-8"))

        consecutive_index = 0
        for cell_type, node_idcs in cells.items():
            # tagEntity(int) dimEntity(int) typeEle(int) numElements(unsigned long)
            fh.write(
                "{} {} {} {}\n".format(
                    1,  # tag
                    _geometric_dimension[cell_type],
                    _meshio_to_gmsh_type[cell_type],
                    node_idcs.shape[0],
                ).encode("utf-8")
            )
            # increment indices by one to conform with gmsh standard
            idcs = node_idcs + 1

            fmt = " ".join(["{}"] * (num_nodes_per_cell[cell_type] + 1)) + "\n"
            for idx in idcs:
                fh.write(fmt.format(consecutive_index, *idx).encode("utf-8"))
                consecutive_index += 1

    fh.write("$EndElements\n".encode("utf-8"))
    return


_geometric_dimension = {
    "line": 1,
    "triangle": 2,
    "quad": 2,
    "tetra": 3,
    "hexahedron": 3,
    "wedge": 3,
    "pyramid": 3,
    "line3": 1,
    "triangle6": 2,
    "quad9": 2,
    "tetra10": 3,
    "hexahedron27": 3,
    "wedge18": 3,
    "pyramid14": 3,
    "vertex": 0,
    "quad8": 2,
    "hexahedron20": 3,
    "triangle10": 2,
    "triangle15": 2,
    "triangle21": 2,
    "line4": 1,
    "line5": 1,
    "line6": 1,
    "tetra20": 3,
    "tetra35": 3,
    "tetra56": 3,
    "quad16": 2,
    "quad25": 2,
    "quad36": 2,
    "triangle28": 2,
    "triangle36": 2,
    "triangle45": 2,
    "triangle55": 2,
    "triangle66": 2,
    "quad49": 2,
    "quad64": 2,
    "quad81": 2,
    "quad100": 2,
    "quad121": 2,
    "line7": 1,
    "line8": 1,
    "line9": 1,
    "line10": 1,
    "line11": 1,
    "tetra84": 3,
    "tetra120": 3,
    "tetra165": 3,
    "tetra220": 3,
    "tetra286": 3,
    "wedge40": 3,
    "wedge75": 3,
    "hexahedron64": 3,
    "hexahedron125": 3,
    "hexahedron216": 3,
    "hexahedron343": 3,
    "hexahedron512": 3,
    "hexahedron729": 3,
    "hexahedron1000": 3,
    "wedge126": 3,
    "wedge196": 3,
    "wedge288": 3,
    "wedge405": 3,
    "wedge550": 3,
}

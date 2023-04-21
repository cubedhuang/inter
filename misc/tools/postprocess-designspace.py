import sys, os, os.path, re, argparse
import defcon
from multiprocessing import Pool
from fontTools.designspaceLib import DesignSpaceDocument
from ufo2ft.filters import loadFilters
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'tools')))
from common import getGitHash, getVersion
from postprocess_instance_ufo import ufo_set_wws


OPT_EDITABLE = False  # --editable


def update_version(ufo):
  version = getVersion()
  buildtag, buildtagErrs = getGitHash()
  now = datetime.utcnow()
  if buildtag == "" or len(buildtagErrs) > 0:
    buildtag = "src"
    print("warning: getGitHash() failed: %r" % buildtagErrs, file=sys.stderr)
  versionMajor, versionMinor = [int(num) for num in version.split(".")]
  ufo.info.versionMajor = versionMajor
  ufo.info.versionMinor = versionMinor
  ufo.info.year = now.year
  ufo.info.openTypeNameVersion = "Version %d.%03d;git-%s" % (versionMajor, versionMinor, buildtag)
  psFamily = re.sub(r'\s', '', ufo.info.familyName)
  psStyle = re.sub(r'\s', '', ufo.info.styleName)
  ufo.info.openTypeNameUniqueID = "%s-%s:%d:%s" % (psFamily, psStyle, now.year, buildtag)
  ufo.info.openTypeHeadCreated = now.strftime("%Y/%m/%d %H:%M:%S")


def fix_opsz_range(designspace):
  # TODO: find extremes by looking at the source
  for a in designspace.axes:
    if a.tag == "opsz":
      a.minimum = 14
      a.maximum = 32
      break
  return designspace


def fix_wght_range(designspace):
  for a in designspace.axes:
    if a.tag == "wght":
      a.minimum = 100
      a.maximum = 900
      break
  return designspace


def should_decompose_glyph(g):
  if not g.components or len(g.components) == 0:
    return False

  # Does the component have non-trivial transformation? (i.e. scaled or skewed)
  # Example of no transformation: (identity matrix)
  #   (1, 0, 0, 1, 0, 0)    no scale or offset
  # Example of simple offset transformation matrix:
  #   (1, 0, 0, 1, 20, 30)  20 x offset, 30 y offset
  # Example of scaled transformation matrix:
  #   (-1.0, 0, 0.3311, 1, 1464.0, 0)  flipped x axis, sheered and offset
  # Matrix order:
  #   (x_scale, x_skew, y_skew, y_scale, x_offs, y_offs)
  for cn in g.components:
    # if g.name == 'dotmacron.lc':
    #   print(f"{g.name} cn {cn.baseGlyph}", cn.transformation)
    # Check if transformation is not identity (ignoring x & y offset)
    m = cn.transformation
    if m[0] + m[1] + m[2] + m[3] != 2.0:
      return True

  return False


def copy_component_anchors(font, g):
  # do nothing if there are no components or if g has anchors already
  if not g.components or len(g.anchors) > 0:
    return

  anchor_names = set()
  for cn in g.components:
    if cn.transformation[1] != 0.0 or cn.transformation[2] != 0.0:
      print(f"TODO: support transformations with skew ({g.name})")
      return
    cn_g = font[cn.baseGlyph]
    copy_component_anchors(font, cn_g)  # depth first
    for a in cn_g.anchors:
      # Check if there are multiple components with achors with the same name.
      # Don't copy any anchors if there are duplicate "_..." anchors
      if a.name in anchor_names and len(a.name) > 1 and a.name[0] == '_':
        return
      anchor_names.add(a.name)

  if len(anchor_names) == 0:
    return

  anchor_names.clear()
  for cn in g.components:
    for a in font[cn.baseGlyph].anchors:
      if a.name in anchor_names:
        continue
      anchor_names.add(a.name)
      a2 = defcon.Anchor(glyph=g, anchorDict=a.copy())
      m = cn.transformation # (x_scale, x_skew, y_skew, y_scale, x_offs, y_offs)
      a2.x += m[4] * m[0]
      a2.y += m[5] * m[3]
      g.appendAnchor(a2)


def find_glyphs_to_decompose(designspace_source):
  glyph_names = set()
  # print("find_glyphs_to_decompose inspecting %r" % designspace_source.name)
  font = defcon.Font(designspace_source.path)
  for g in font:
    # copy_component_anchors(font, g)
    if should_decompose_glyph(g):
      glyph_names.add(g.name)
  font.save(designspace_source.path)
  return list(glyph_names)


def set_ufo_filter(ufo, **filter_dict):
  filters = ufo.lib.setdefault("com.github.googlei18n.ufo2ft.filters", [])
  for i in range(len(filters)):
    if filters[i].get("name") == filter_dict["name"]:
      filters[i] = filter_dict
      return
  filters.append(filter_dict)


def del_ufo_filter(ufo, name):
  filters = ufo.lib.get("com.github.googlei18n.ufo2ft.filters")
  if not filters:
    return
  for i in range(len(filters)):
    if filters[i].get("name") == name:
      filters.pop(i)
      return


def update_source_ufo(ufo_file, glyphs_to_decompose):
  print(f"update {os.path.basename(ufo_file)}")

  ufo = defcon.Font(ufo_file)
  update_version(ufo)

  set_ufo_filter(ufo, name="decomposeComponents", include=glyphs_to_decompose)

  # decompose now, up front, instead of later when compiling fonts
  if not OPT_EDITABLE:
    preFilters, postFilters = loadFilters(ufo)
    for filter in preFilters:
      filter(ufo)
    for filter in postFilters:
      filter(ufo)
    # del_ufo_filter(ufo, "decomposeComponents")
    del ufo.lib["com.github.googlei18n.ufo2ft.filters"]

  ufo_set_wws(ufo) # Fix missing WWS entries for Display fonts
  ufo.save(ufo_file)


def update_sources(designspace):
  with Pool() as p:
    sources = [source for source in designspace.sources]
    # sources = [s for s in sources if s.name == "Inter Thin"] # DEBUG
    glyphs_to_decompose = set()
    for glyph_names in p.map(find_glyphs_to_decompose, sources):
      glyphs_to_decompose.update(glyph_names)
    glyphs_to_decompose = list(glyphs_to_decompose)
    # print("glyphs marked to be decomposed: %s" % ', '.join(glyphs_to_decompose))
    source_files = list(set([s.path for s in sources]))
    p.starmap(update_source_ufo, [(path, glyphs_to_decompose) for path in source_files])
  return designspace


def main(argv):
  ap = argparse.ArgumentParser(description=
    'Fixup designspace and source UFOs after they are generated by fontmake from Glyphs source')
  ap.add_argument('--editable', action='store_true',
    help="Generate UFOs suitable for further editing (don't apply filters)")
  ap.add_argument("designspace", help="Path to designspace file")

  args = ap.parse_args()
  OPT_EDITABLE = args.editable

  designspace = DesignSpaceDocument.fromfile(args.designspace)
  designspace = fix_opsz_range(designspace)
  designspace = fix_wght_range(designspace)
  designspace = update_sources(designspace)
  designspace.write(args.designspace)


if __name__ == '__main__':
  main(sys.argv)

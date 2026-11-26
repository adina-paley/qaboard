# Annotations, and collections from code (milestone 2 of 5)

Milestone 1 (`inputs-and-collections.md`) shipped the Inputs page, collections saved from a selection, their pages,
the `collections:` key in batch files and `qaboard.collections.get()`. This page is the contract for milestone 2: what
ships, the data model, the API, and what waits. Backend, CLI and web app are built against it.

Milestone 2 in the plan: "Collections in YAML and through the Python API; drawing and labelling rectangle annotations
in the viewer, groups and namespaces, search, external annotations alongside". Useful right away for scripts and
training code, and for marking regions.

## What ships

- **Rectangle annotations on inputs**, drawn and labelled in the Inputs viewer, saved in QA-Board's database. A
  rectangle is in the input's own pixels: `x, y, w, h` (integers, `w, h > 0`, inside the image when its size is known).
  It shows on the input and on every image of the same size made from it (runs' outputs), scaled to other sizes like
  today's regions.
- **Annotation groups**: every annotation is in one group, named like a path: `af-team/train-patches`, `isp/defects`.
  A group belongs to a project (its subprojects see it, like collections), and optionally to a team (a QA-Board group):
  then its members edit it. Personal groups (`~adina/sandbox`) are their creator's only.
- **Editing with history**: people who can edit a group add, move, resize, relabel and delete its annotations. Every
  change is kept (who, when, what it was before) and shown per annotation. Deleting hides; it's never lost.
- **Labels and fields**: each annotation has labels (free text, autocompleted from the group's labels), optional free
  fields (`{key: value}`), and how it was made: `manual`, `script`, `model:<name>`, `import`.
- **Search**: the Inputs page filters by annotation group and label (with counts), and "has annotations"; thumbnails
  show how many annotations an input has in the groups shown.
- **External annotations alongside**: the read-only sources sites already install (`backend/annotations.py`) show in
  the same viewer, beside QA-Board's groups. Copying them into a QA-Board group waits (milestone 3).
- **In run viewers too**: the run's image viewer offers QA-Board's annotation groups as region sets (read-only there),
  on the run's input and on its outputs.
- **Collections from code**: `qaboard.collections.save(name, inputs, ...)` makes a collection or its next version from
  a script, and records where it came from (script, command line, host, git commit). `qa collections save` does the
  same from a YAML file or a list of paths, so a collection can live in a repository.

Not in milestone 2: annotations in collections (members with a region), per-group rules (snapping, fixed sizes,
allowed labels), annotating from code (`qaboard.inputs.annotate()`), copying external annotations, exporting crops,
`inputs.load()` and the training cache: milestone 3. Masks, polygons and points: not planned. Sampling and balancing
collections: later, in the SDK.

## Data model

```
annotation_groups
  id            serial
  project_id    str FK projects.id ON DELETE CASCADE
  name          str             "af-team/train-patches"; "~user/..." for personal ones (normalized like collections)
  description   text
  team_id       int FK groups.id ON DELETE SET NULL, null    the team whose members edit it
  personal      bool            true for "~user/..." names: only created_by sees and edits it
  created_by    int FK users.id ON DELETE SET NULL
  created_at, updated_at
  archived_at   timestamp null  hidden from lists and pickers; its annotations stay readable
  unique (project_id, name)

annotations
  id            bigserial
  group_id      int FK annotation_groups.id ON DELETE CASCADE
  test_input_id int FK test_inputs.id ON DELETE CASCADE
  x, y, w, h    int             in the input's pixels; w, h > 0
  image_width, image_height  int null   the input's size when drawn, if known (to scale on other sizes)
  labels        text[]          default '{}'        GIN index
  fields        jsonb           default '{}'        free key/values, ≤ 4 KB
  method        str             manual | script | model:<name> | import
  output_id     int FK outputs.id ON DELETE SET NULL, null   the run that made it, for script/model
  created_by, created_at, updated_by, updated_at
  deleted_at    timestamp null  deleted: hidden, kept with its history
  version       int             +1 per change; edits send the version they saw (409 if stale)
  index (test_input_id) WHERE deleted_at IS NULL, index (group_id, test_input_id)

annotation_changes        the history: one row per change, never updated
  id bigserial, annotation_id FK ON DELETE CASCADE, version int, action create|update|delete|restore
  changed_by, changed_at, before jsonb (null for create), after jsonb

collection_versions.source  jsonb null   (new column) where a version saved from code came from:
  {script, argv, host, cwd, git: {repository, commit, dirty}}
```

## Permissions

New permission `annotations.write`: (`developer`, `act`), like `collections.write`.

- **See** a group and its annotations: `project.read` on the group's project or a subproject; personal groups only
  their creator.
- **Create a group**: `annotations.write` on the project; personal groups (`~me/...`): anyone signed in who reads it.
- **Edit annotations of a group**: its creator; members of its team when it has one; otherwise anyone with
  `annotations.write` on its project. Personal groups: only their creator.
- **Rename, describe, set the team, archive a group**: its creator or the project's owners.
- Annotations of an input are only shown to people who can read a project that used the input.

Every change is audited (`annotation.create|update|delete|restore`, `annotation_group.*`) besides the history.

## API (under `/api/v1`, in `backend/backend/openapi.yaml`)

| Method | Path | What |
|---|---|---|
| GET | `/annotation-groups?project=&archived=0` | `{groups: [Group], can_create}` own and inherited, personal ones of the user |
| POST | `/annotation-groups` `{project, name, description?, team?}` | 201 `Group`, 409 if the name exists |
| PATCH | `/annotation-groups/<id>` `{name?, description?, team?, archived?}` | |
| GET | `/inputs/<id>/annotations?project=&groups=1,2` | `{annotations: [Annotation], groups: [Group]}` the groups visible from the project |
| POST | `/inputs/<id>/annotations` `{group, x, y, w, h, labels?, fields?, method?, image_width?, image_height?}` | 201 `Annotation` |
| PATCH | `/annotations/<id>` `{version, x?, y?, w?, h?, labels?, fields?}` | 409 `stale_version` with the current one |
| DELETE | `/annotations/<id>?version=` | soft delete; POST `/annotations/<id>/restore` undoes it |
| GET | `/annotations/<id>/history` | `{changes: [{version, action, changed_by, changed_at, before, after}]}` |
| GET | `/annotation-groups/<id>/labels?q=` | `{labels: [{label, count}]}` for autocomplete |
| GET | `/inputs?...&annotation_group=&label=&annotated=1` | the Inputs list filters; facets add `annotation_groups: [{id, name, count}]`, `labels: [{label, count}]`; each Input has `annotations: n` |
| POST | `/collections/save` `{project, name, inputs: [id or {database, path}], notes?, description?, base_version?, source?}` | from code: creates the collection or its next version (unknown paths are added like `POST /inputs`); 200 with `unchanged: true` when the inputs equal the latest version's |

`Group`: `{id, project, name, description, team: {id, name} | null, personal, created_by, created_at, archived,
can_edit, inherited, count}`. `Annotation`: `{id, group, input, x, y, w, h, image_width, image_height, labels, fields,
method, output, created_by, created_at, updated_by, updated_at, version, can_edit}`.

Limits: 10,000 annotations per input and group, 50 labels per annotation of ≤ 100 characters each, names ≤ 200.

## CLI and SDK

```python
import qaboard
collection = qaboard.collections.save("night/hard-cases", ["night/street/IMG_0042.raw", ...],
                                      database="/mnt/captures", notes="from the failure triage script")
collection.version   # 3, or the same as before when nothing changed
```

- `save(name, inputs, *, database=None, project=None, notes=None, description=None, base_version=None)`: `inputs` are
  paths (relative to `database`, or absolute), `CollectionInput`s, or a `Collection`. Records `source` automatically.
  Returns the `Collection` saved. Raises `CollectionConflict` when `base_version` isn't the latest.
- `qa collections save <file.yaml | list.txt> [--name N] [--database D] [--notes ...] [--dryrun]`:

  ```yaml
  # collections/night/hard-cases.yaml in a repository
  name: night/hard-cases          # optional: by default the file's path under a `collections/` folder
  description: Street lights that fool contrast AF
  database: /mnt/captures         # optional, like batch files
  inputs:
    - night/street/IMG_0042.raw
    - night/street/IMG_0051.raw
  ```

  A `.txt` lists one path per line. `--dryrun` prints what would change against the latest version (added, removed);
  an unchanged file saves nothing. Globs work like in batch files.

## Web app

- **Inputs viewer, Annotations panel**: a side panel listing the input's annotations by group (QA-Board groups first,
  then external sources), each toggleable; a group picker for drawing ("Draw into: af-team/train-patches", with "New
  group…"); a Draw tool (`R`): drag a rectangle; select one to move, resize by its handles, type exact `x, y, w, h`,
  edit labels (autocomplete) and fields, delete (`Delete`, with Undo in the toast); per-annotation history. Read-only
  groups and external sources show without handles. Unsaved edits are never lost silently.
- **The image viewer** (`webapp/src/image-viewer/`, standalone) gets editable regions as an opt-in feature: props for
  the regions to show as editable, and callbacks on create/change/select/delete. It stays free of app imports.
- **Inputs page**: "Annotations" filters (group, label, has annotations) beside folders and databases, with counts;
  a small count badge on annotated thumbnails; the URL keeps them.
- **Run viewers**: a "QA-Board annotations" region provider listing the groups visible from the run's project.
- **Groups**: managed from the viewer's group picker and a small dialog (name, description, team, archive); no page of
  their own in milestone 2.
- Collection pages show the source of versions saved from code ("Saved by a script: tools/triage.py at a1b2c3d on host").

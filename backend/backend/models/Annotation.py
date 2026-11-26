"""
Annotations of inputs kept by QA-Board (docs/design/annotations.md): rectangles in an input's pixels, in groups.

- annotation_groups: named like paths ("af-team/train-patches", "~adina/sandbox" for personal ones), of a project
  (its subprojects see them, like collections), optionally of a team (a QA-Board group: its members edit them).
- annotations: deleting one hides it (deleted_at), never removes it. `version` grows by one per change: edits send
  the version they saw, and are refused when someone changed it meanwhile.
- annotation_changes: the history, one row per change, written in the same transaction, never updated.

The API reads and writes them with SQL (backend/annotation_catalog.py). The migration f3a5c7e9b1d2 creates them: keep
both the same, since new databases get the tables from these models (Base.metadata.create_all).
Annotations of other apps, read-only, are backend/annotations.py's sources.
"""
from backend import clock

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, CheckConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

from backend.models import Base


class AnnotationGroup(Base):
  __tablename__ = 'annotation_groups'

  id = Column(Integer, primary_key=True)
  project_id = Column(String(), ForeignKey('projects.id', ondelete='CASCADE'), nullable=False)
  # normalized like collections' names; "~user/..." for personal ones
  name = Column(String(), nullable=False)
  description = Column(Text(), nullable=False, default='', server_default='')
  # the team whose members edit it
  team_id = Column(Integer, ForeignKey('groups.id', ondelete='SET NULL'), nullable=True)
  # "~user/..." names: only created_by sees and edits it
  personal = Column(Boolean, nullable=False, default=False, server_default=text('false'))
  created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
  created_at = Column(DateTime, nullable=False, default=clock.utcnow)
  updated_at = Column(DateTime, nullable=False, default=clock.utcnow)
  # hidden from lists and pickers; its annotations stay readable
  archived_at = Column(DateTime, nullable=True)

  __table_args__ = (
    UniqueConstraint('project_id', 'name', name='uq_annotation_groups_project_id_name'),
  )


class Annotation(Base):
  __tablename__ = 'annotations'

  id = Column(BigInteger, primary_key=True)
  group_id = Column(Integer, ForeignKey('annotation_groups.id', ondelete='CASCADE'), nullable=False)
  test_input_id = Column(Integer, ForeignKey('test_inputs.id', ondelete='CASCADE'), nullable=False)
  # in the input's pixels
  x = Column(Integer, nullable=False)
  y = Column(Integer, nullable=False)
  w = Column(Integer, nullable=False)
  h = Column(Integer, nullable=False)
  # the input's size when it was drawn, if known: to scale it on images of other sizes
  image_width = Column(Integer, nullable=True)
  image_height = Column(Integer, nullable=True)
  labels = Column(ARRAY(Text()), nullable=False, default=list, server_default=text("'{}'"))
  # free key/values, at most 4 KB of JSON
  fields = Column(JSONB(), nullable=False, default=dict, server_default=text("'{}'"))
  # manual | script | model:<name> | import
  method = Column(String(), nullable=False, default='manual', server_default='manual')
  # the run that made it, for scripts and models
  output_id = Column(Integer, ForeignKey('outputs.id', ondelete='SET NULL'), nullable=True)
  created_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
  created_at = Column(DateTime, nullable=False, default=clock.utcnow)
  updated_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
  updated_at = Column(DateTime, nullable=False, default=clock.utcnow)
  # deleted: hidden, kept with its history
  deleted_at = Column(DateTime, nullable=True)
  # +1 per change
  version = Column(Integer, nullable=False, default=1, server_default='1')

  __table_args__ = (
    CheckConstraint('w > 0 AND h > 0', name='ck_annotations_size'),
    # an input's annotations (the viewer, the Inputs page's filters and counts: EXISTS per input)
    Index('ix_annotations_live_test_input_id', 'test_input_id', postgresql_where=text('deleted_at IS NULL')),
    # a group's annotations, by input (counts, labels)
    Index('ix_annotations_group_id_test_input_id', 'group_id', 'test_input_id'),
    Index('ix_annotations_labels', 'labels', postgresql_using='gin'),
    # deleting runs (the foreign key's check): few annotations have one
    Index('ix_annotations_output_id', 'output_id', postgresql_where=text('output_id IS NOT NULL')),
  )


class AnnotationChange(Base):
  """The history of an annotation: one row per change, never updated"""
  __tablename__ = 'annotation_changes'

  id = Column(BigInteger, primary_key=True)
  annotation_id = Column(BigInteger, ForeignKey('annotations.id', ondelete='CASCADE'), nullable=False)
  # the annotation's version after the change
  version = Column(Integer, nullable=False)
  # create | update | delete | restore
  action = Column(String(), nullable=False)
  changed_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
  changed_at = Column(DateTime, nullable=False, default=clock.utcnow)
  # null for create
  before = Column(JSONB(), nullable=True)
  after = Column(JSONB(), nullable=False)

  __table_args__ = (
    Index('ix_annotation_changes_annotation_id_version', 'annotation_id', 'version'),
  )

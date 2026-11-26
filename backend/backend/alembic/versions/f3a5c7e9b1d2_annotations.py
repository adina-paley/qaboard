"""Annotations of inputs, in groups, with their history; where collection versions saved from code came from

New tables, empty: no backfill, nothing built CONCURRENTLY. See docs/design/annotations.md and backend/models/Annotation.py.

Revision ID: f3a5c7e9b1d2
Revises: b8d0f2a4c6e1
Create Date: 2026-11-26 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'f3a5c7e9b1d2'
down_revision = 'b8d0f2a4c6e1'
branch_labels = None
depends_on = None

NOW = sa.text("(now() at time zone 'utc')")


def has_table(table):
  return sa.inspect(op.get_bind()).has_table(table)


def columns_of(table):
  return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
  # The app creates missing tables when it starts (Base.metadata.create_all): they may be there already, empty
  if not has_table('annotation_groups'):
    op.create_table(
      'annotation_groups',
      sa.Column('id', sa.Integer(), primary_key=True),
      sa.Column('project_id', sa.String(), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
      sa.Column('name', sa.String(), nullable=False),
      sa.Column('description', sa.Text(), nullable=False, server_default=''),
      sa.Column('team_id', sa.Integer(), sa.ForeignKey('groups.id', ondelete='SET NULL'), nullable=True),
      # "~user/..." names: only created_by sees and edits it
      sa.Column('personal', sa.Boolean(), nullable=False, server_default=sa.false()),
      sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
      sa.Column('created_at', sa.DateTime(), nullable=False, server_default=NOW),
      sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=NOW),
      sa.Column('archived_at', sa.DateTime(), nullable=True),
      sa.UniqueConstraint('project_id', 'name', name='uq_annotation_groups_project_id_name'),
    )

  if not has_table('annotations'):
    op.create_table(
      'annotations',
      sa.Column('id', sa.BigInteger(), primary_key=True),
      sa.Column('group_id', sa.Integer(), sa.ForeignKey('annotation_groups.id', ondelete='CASCADE'), nullable=False),
      sa.Column('test_input_id', sa.Integer(), sa.ForeignKey('test_inputs.id', ondelete='CASCADE'), nullable=False),
      sa.Column('x', sa.Integer(), nullable=False),
      sa.Column('y', sa.Integer(), nullable=False),
      sa.Column('w', sa.Integer(), nullable=False),
      sa.Column('h', sa.Integer(), nullable=False),
      sa.Column('image_width', sa.Integer(), nullable=True),
      sa.Column('image_height', sa.Integer(), nullable=True),
      sa.Column('labels', postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
      sa.Column('fields', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
      sa.Column('method', sa.String(), nullable=False, server_default='manual'),
      sa.Column('output_id', sa.Integer(), sa.ForeignKey('outputs.id', ondelete='SET NULL'), nullable=True),
      sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
      sa.Column('created_at', sa.DateTime(), nullable=False, server_default=NOW),
      sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
      sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=NOW),
      sa.Column('deleted_at', sa.DateTime(), nullable=True),
      sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
      sa.CheckConstraint('w > 0 AND h > 0', name='ck_annotations_size'),
    )
  # an input's annotations: the viewer, and the Inputs page's filters and counts (EXISTS per input)
  op.create_index('ix_annotations_live_test_input_id', 'annotations', ['test_input_id'],
                  postgresql_where=sa.text('deleted_at IS NULL'), if_not_exists=True)
  op.create_index('ix_annotations_group_id_test_input_id', 'annotations', ['group_id', 'test_input_id'], if_not_exists=True)
  op.create_index('ix_annotations_labels', 'annotations', ['labels'], postgresql_using='gin', if_not_exists=True)
  # deleting runs checks the foreign key: few annotations have a run
  op.create_index('ix_annotations_output_id', 'annotations', ['output_id'],
                  postgresql_where=sa.text('output_id IS NOT NULL'), if_not_exists=True)

  if not has_table('annotation_changes'):
    op.create_table(
      'annotation_changes',
      sa.Column('id', sa.BigInteger(), primary_key=True),
      sa.Column('annotation_id', sa.BigInteger(), sa.ForeignKey('annotations.id', ondelete='CASCADE'), nullable=False),
      sa.Column('version', sa.Integer(), nullable=False),
      sa.Column('action', sa.String(), nullable=False),
      sa.Column('changed_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
      sa.Column('changed_at', sa.DateTime(), nullable=False, server_default=NOW),
      sa.Column('before', postgresql.JSONB(), nullable=True),
      sa.Column('after', postgresql.JSONB(), nullable=False),
    )
  op.create_index('ix_annotation_changes_annotation_id_version', 'annotation_changes', ['annotation_id', 'version'], if_not_exists=True)

  # where a version saved from code came from (POST /collections/save). No default: nothing to rewrite.
  if 'source' not in columns_of('collection_versions'):
    op.add_column('collection_versions', sa.Column('source', postgresql.JSONB(), nullable=True))


def downgrade():
  op.drop_column('collection_versions', 'source')
  op.drop_table('annotation_changes')
  op.drop_table('annotations')
  op.drop_table('annotation_groups')

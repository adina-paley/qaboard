// The Annotations panel of the Inputs viewer (docs/design/annotations.md, "Web app"): the input's annotations by
// group, QA-Board's first, then the site's external sources (read-only); which groups show; which group rectangles
// are drawn into; the selected annotation's rectangle, labels, fields, how it was made and its history.
// The state is useAnnotationEditor's: the image viewer edits the same annotations.
//
// Typed edits are saved on Enter or when the field loses the focus (also when the panel closes): never lost silently.
// Esc in a field puts back what was there.
import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import styled from "styled-components";
import {
  AnchorButton, Button, Callout, Checkbox, Classes, Colors, Dialog, DialogBody, DialogFooter, FormGroup, HTMLSelect, Icon,
  InputGroup, Intent, Radio, RadioGroup, Switch, Tag, TagInput, TextArea, Tooltip,
} from "@blueprintjs/core";
import { DateTime } from "luxon";

import { errorMessage } from "../api/http";
import { toaster } from "../toaster";
import { clampRect, sameRect } from "../image-viewer/regionEditing";
import { usePrefsStore } from "../stores/prefs";
import { isGroupShown } from "../viewers/images/regionProviders";
import {
  annotationHistoryQuery, groupLabelsQuery, describe_change, method_label, person, clean_labels, parse_field_value,
  group_name_problem, normalize_group_name, personal_prefix, is_saved_id, annotation_label,
} from "./annotations";


const Panel = styled.section`
  display: flex;
  flex-direction: column;
  gap: 12px;
  font-size: 13px;

  .qa-annotations-tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .qa-annotations-tools label {
    color: ${Colors.GRAY4};
    font-size: 12px;
  }
  .qa-annotations-tools .${Classes.HTML_SELECT} {
    flex: 1 1 140px;
    min-width: 0;
  }
  .qa-group {
    border-radius: 3px;
  }
  .qa-group-row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }
  .qa-group-row .${Classes.CONTROL} {
    flex: 1;
    min-width: 0;
    margin: 0;
  }
  .qa-group-name {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .qa-swatch {
    flex: none;
    width: 10px;
    height: 10px;
    border-radius: 2px;
    background: var(--group-color, ${Colors.BLUE4});
  }
  .qa-count {
    color: ${Colors.GRAY4};
    font-variant-numeric: tabular-nums;
    font-size: 12px;
  }
  .qa-annotation-list {
    list-style: none;
    margin: 2px 0 6px 22px;
    padding: 0;
  }
  .qa-annotation-item {
    display: flex;
    gap: 6px;
    width: 100%;
    padding: 2px 6px;
    border: 0;
    border-radius: 3px;
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: 12px;
    text-align: left;
    cursor: pointer;
  }
  .qa-annotation-item:hover, .qa-annotation-item:focus-visible {
    background: rgba(255, 255, 255, 0.06);
  }
  .qa-annotation-item[aria-current="true"] {
    background: rgba(76, 144, 240, 0.25);
  }
  .qa-annotation-item .qa-item-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .qa-muted {
    color: ${Colors.GRAY4};
  }
  .qa-details {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 10px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.04);
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08);
  }
  .qa-details-title {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    font-weight: 600;
  }
  .qa-rect-fields {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
  }
  .qa-rect-fields label {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 11px;
    color: ${Colors.GRAY4};
  }
  .qa-rect-fields input {
    font-variant-numeric: tabular-nums;
  }
  .qa-field-row {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(0, 3fr) auto;
    gap: 4px;
    margin-bottom: 4px;
  }
  .qa-problem {
    color: ${Colors.RED5};
    font-size: 12px;
  }
  .qa-history {
    list-style: none;
    margin: 4px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 12px;
  }
  .qa-history-meta {
    color: ${Colors.GRAY4};
  }
  dl {
    font-size: 12px;
  }
`;

const full_date = date => date ? DateTime.fromISO(date).toLocaleString(DateTime.DATETIME_MED) : '';
const relative = date => date ? DateTime.fromISO(date).toRelative() : '';
const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
const Key = ({ children }) => <kbd className={Classes.KEY}>{children}</kbd>;
// annotations listed under each group, then "and N more": an input can have thousands
const MAX_LISTED = 50;


// A value users type, saved on Enter or blur, and when the field goes away. Esc puts back what was there.
// parse(text) => {value} or {error}. commit(value) is called when the text changed.
const useDraft = (text, commit, { parse = value => ({ value }), what = 'your change' } = {}) => {
  const [draft, setDraft] = useState(null);
  const [problem, setProblem] = useState(null);
  const latest = useRef(null);
  useLayoutEffect(() => { latest.current = { draft, text, commit, parse }; });
  const save = () => {
    if (draft === null) return true;
    if (draft === text) {
      setDraft(null);
      return true;
    }
    const parsed = parse(draft);
    if (parsed.error) {
      setProblem(parsed.error);
      return false;
    }
    setDraft(null);
    setProblem(null);
    commit(parsed.value);
    return true;
  };
  // unmounted while typing, e.g. another annotation selected or the viewer closed: saved, or said
  useEffect(() => () => {
    const { draft, text, commit, parse } = latest.current ?? {};
    if (draft === null || draft === undefined || draft === text) return;
    const parsed = parse(draft);
    if (parsed.error) toaster.show({ message: `${what} wasn't saved: ${parsed.error}`, intent: Intent.WARNING, timeout: 8000 });
    else commit(parsed.value);
    // oxlint-disable-next-line react/exhaustive-deps
  }, []);
  return {
    value: draft ?? text,
    dirty: draft !== null && draft !== text,
    problem,
    onChange: event => {
      setDraft(event.target.value);
      setProblem(null);
    },
    onBlur: save,
    onKeyDown: event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        save();
      } else if (event.key === 'Escape' && draft !== null) {
        // not the viewer's Esc
        event.preventDefault();
        event.stopPropagation();
        setDraft(null);
        setProblem(null);
      }
    },
  };
};

const integer = (min, name) => text => {
  const value = Number(String(text).trim());
  if (!/^-?\d+$/.test(String(text).trim())) return { error: `${name} is a whole number of pixels` };
  if (value < min) return { error: `${name} is at least ${min}` };
  return { value };
};

const NumberField = ({ name, label, value, min, onCommit, disabled }) => {
  const field = useDraft(String(value ?? ''), onCommit, { parse: integer(min, name), what: `The new ${name}` });
  return <label>
    {label}
    <input
      className={`${Classes.INPUT} ${Classes.SMALL} ${field.problem ? Classes.INTENT_DANGER : ''}`}
      inputMode="numeric"
      aria-label={`${label} (pixels)`}
      aria-invalid={!!field.problem}
      title={field.problem ?? undefined}
      disabled={disabled}
      value={field.value}
      onChange={field.onChange}
      onBlur={field.onBlur}
      onKeyDown={field.onKeyDown}
    />
  </label>;
};

// x, y, w, h in the input's pixels: kept inside the image when its size is known
const RectFields = ({ annotation, onChange, disabled }) => {
  const [note, setNote] = useState(null);
  const bounds = annotation.image_width && annotation.image_height ? { width: annotation.image_width, height: annotation.image_height } : null;
  const commit = key => value => {
    const current = { x: annotation.x, y: annotation.y, w: annotation.w, h: annotation.h };
    const typed = { ...current, [key]: value };
    const rect = clampRect(typed, bounds);
    setNote(!sameRect(rect, typed) ? `Kept inside the image (${bounds.width}×${bounds.height}): ${rect.w}×${rect.h} at (${rect.x}, ${rect.y})` : null);
    const changes = Object.fromEntries(Object.entries(rect).filter(([k, v]) => current[k] !== v));
    if (Object.keys(changes).length > 0) onChange(changes);
  };
  return <div>
    <div className="qa-rect-fields" role="group" aria-label="Rectangle, in the input's pixels">
      <NumberField name="x" label="x" value={annotation.x} min={0} onCommit={commit('x')} disabled={disabled}/>
      <NumberField name="y" label="y" value={annotation.y} min={0} onCommit={commit('y')} disabled={disabled}/>
      <NumberField name="width" label="w" value={annotation.w} min={1} onCommit={commit('w')} disabled={disabled}/>
      <NumberField name="height" label="h" value={annotation.h} min={1} onCommit={commit('h')} disabled={disabled}/>
    </div>
    {note && <div className="qa-muted" role="status" style={{ fontSize: 12, marginTop: 4 }}>{note}</div>}
    {bounds && <div className="qa-muted" style={{ fontSize: 11, marginTop: 2 }}>In the pixels of the input ({bounds.width}×{bounds.height})</div>}
  </div>;
};

// Labels, autocompleted from the group's
const LabelsField = ({ annotation, group, onChange, disabled }) => {
  const id = useId();
  const [text, setText] = useState('');
  const { data: suggestions = [] } = useQuery({ ...groupLabelsQuery(group?.id, text), enabled: !!group && !disabled });
  const labels = annotation.labels ?? [];
  const add = values => {
    const next = clean_labels([...labels, ...values]);
    if (next.length !== labels.length) onChange(next);
    setText('');
  };
  return <FormGroup label="Labels" labelFor={`${id}-labels`} style={{ margin: 0 }}>
    <TagInput
      values={labels}
      onAdd={add}
      onRemove={(_, index) => onChange(labels.filter((__, i) => i !== index))}
      inputValue={text}
      onInputChange={event => setText(event.target.value)}
      addOnBlur
      addOnPaste
      separator={/[,\n\r]/}
      disabled={disabled}
      placeholder={labels.length ? '' : disabled ? 'No labels' : 'Add labels: type, then Enter'}
      inputProps={{ id: `${id}-labels`, list: `${id}-suggestions`, autoComplete: 'off' }}
      tagProps={{ minimal: true }}
      fill
    />
    <datalist id={`${id}-suggestions`}>
      {suggestions.filter(s => !labels.includes(s.label)).slice(0, 20).map(s => <option key={s.label} value={s.label}>{plural(s.count, 'annotation')}</option>)}
    </datalist>
  </FormGroup>;
};

// what an <input> takes of a draft
const draft_props = field => ({ value: field.value, onChange: field.onChange, onBlur: field.onBlur, onKeyDown: field.onKeyDown, 'aria-invalid': !!field.problem, title: field.problem ?? undefined });

const field_text = value => value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);

const FieldRow = ({ name, value, onRename, onValue, onRemove, disabled }) => {
  const key_field = useDraft(name, onRename, { parse: text => text.trim() ? { value: text.trim() } : { error: 'a field needs a name' }, what: `The field's name ${name}` });
  const value_field = useDraft(field_text(value), text => onValue(parse_field_value(text)), { what: `The value of ${name}` });
  return <div className="qa-field-row">
    <input className={`${Classes.INPUT} ${Classes.SMALL} ${key_field.problem ? Classes.INTENT_DANGER : ''}`} aria-label="Field name" disabled={disabled} {...draft_props(key_field)}/>
    <input className={`${Classes.INPUT} ${Classes.SMALL}`} aria-label={`Value of ${name}`} disabled={disabled} {...draft_props(value_field)}/>
    {!disabled && <Button size="small" variant="minimal" icon="cross" aria-label={`Remove the field ${name}`} onClick={onRemove}/>}
  </div>;
};

// Free key/values: {key: value}. Numbers and true/false are saved as such.
const FieldsEditor = ({ fields, onChange, disabled }) => {
  const entries = Object.entries(fields ?? {});
  const [adding, setAdding] = useState(null);
  const set = (from, to, value) => {
    const next = {};
    for (const [k, v] of entries) {
      if (k === from) next[to] = value;
      else if (k !== to) next[k] = v;
    }
    if (from === null) next[to] = value;
    onChange(next);
  };
  const remove = name => onChange(Object.fromEntries(entries.filter(([k]) => k !== name)));
  const add = () => {
    if (!adding?.key.trim()) return setAdding(null);
    set(null, adding.key.trim(), parse_field_value(adding.value));
    setAdding(null);
  };
  const onNewFieldKey = event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      add();
    } else if (event.key === 'Escape') {
      // not the viewer's Esc
      event.preventDefault();
      event.stopPropagation();
      setAdding(null);
    }
  };
  // a field being added is saved when the panel goes away too
  const pending = useRef(null);
  useLayoutEffect(() => { pending.current = adding?.key.trim() ? add : null; });
  useEffect(() => () => pending.current?.(), []);
  return <div>
    <div className={Classes.LABEL} style={{ marginBottom: 4 }}>Fields</div>
    {entries.length === 0 && !adding && <div className="qa-muted" style={{ fontSize: 12, marginBottom: 4 }}>None{disabled ? '' : ': add key/values, e.g. severity: high'}</div>}
    {entries.map(([k, v]) => <FieldRow
      key={k}
      name={k}
      value={v}
      disabled={disabled}
      onRename={to => set(k, to, v)}
      onValue={value => set(k, k, value)}
      onRemove={() => remove(k)}
    />)}
    {adding && <div className="qa-field-row">
      {/* the user just asked for a new field */}
      <input className={`${Classes.INPUT} ${Classes.SMALL}`} aria-label="New field name" placeholder="name" autoFocus value={adding.key} onChange={e => setAdding({ ...adding, key: e.target.value })} onKeyDown={onNewFieldKey}/>
      <input className={`${Classes.INPUT} ${Classes.SMALL}`} aria-label="New field value" placeholder="value" value={adding.value} onChange={e => setAdding({ ...adding, value: e.target.value })} onBlur={() => adding.key.trim() && add()} onKeyDown={onNewFieldKey}/>
      <Button size="small" variant="minimal" icon="tick" aria-label="Add the field" onClick={add}/>
    </div>}
    {!disabled && !adding && <Button size="small" variant="minimal" icon="add" text="Add field" onClick={() => setAdding({ key: '', value: '' })}/>}
  </div>;
};

const History = ({ annotation }) => {
  const [open, setOpen] = useState(false);
  const saved = is_saved_id(annotation.id);
  const { data: changes, isPending, error } = useQuery({ ...annotationHistoryQuery(annotation.id, annotation.version), enabled: open && saved });
  return <div>
    <Button size="small" variant="minimal" icon={open ? 'chevron-down' : 'chevron-right'} text="History" aria-expanded={open} disabled={!saved} onClick={() => setOpen(!open)}/>
    {open && (isPending ? <p className={Classes.SKELETON}>Loading its history</p>
      : error ? <p className="qa-problem">Could not load its history: {errorMessage(error)}</p>
      : <ol className="qa-history" aria-label="History">
          {changes.map(change => <li key={change.version}>
            <div className="qa-history-meta" title={full_date(change.changed_at)}>v{change.version} · {person(change.changed_by) ?? 'someone'} · {relative(change.changed_at)}</div>
            {describe_change(change).map((line, i) => <div key={i}>{line}</div>)}
          </li>)}
        </ol>)}
  </div>;
};

// Why users can't edit a group's annotations
const read_only_reason = group => !group ? 'Its group is hidden.'
  : group.personal ? 'It is in someone\'s personal group.'
  : group.team ? `Only members of ${group.team.name} edit ${group.name}.`
  : `Only developers of ${group.project} edit ${group.name}.`;

const AnnotationDetails = ({ editor, annotation }) => {
  const group = editor.group_of(annotation.group);
  const can_edit = !!group?.can_edit && annotation.can_edit !== false;
  const update = changes => editor.update(annotation.id, changes);
  return <div className="qa-details" role="region" aria-label="Selected annotation">
    <div className="qa-details-title">
      <span className="qa-swatch" style={{ '--group-color': group?.color }}/>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={group?.name}>{group?.name ?? 'Annotation'}</span>
      {annotation.saving && <Tag minimal>saving…</Tag>}
      {!can_edit && <Tooltip content={read_only_reason(group)}><Tag minimal icon="lock">read-only</Tag></Tooltip>}
      <span style={{ flex: 1 }}/>
      <Button size="small" variant="minimal" icon="cross" aria-label="Unselect (Esc)" title="Unselect (Esc)" onClick={() => editor.setSelected(null)}/>
    </div>
    <RectFields annotation={annotation} onChange={update} disabled={!can_edit}/>
    <LabelsField annotation={annotation} group={group} onChange={labels => update({ labels })} disabled={!can_edit}/>
    <FieldsEditor fields={annotation.fields} onChange={fields => update({ fields })} disabled={!can_edit}/>
    <dl>
      <dt>Made</dt><dd>{method_label(annotation.method)}</dd>
      {!!annotation.created_at && <><dt>Created</dt><dd title={full_date(annotation.created_at)}>{person(annotation.created_by) ?? 'someone'}, {relative(annotation.created_at)}</dd></>}
      {!!annotation.updated_at && annotation.version > 1 && <><dt>Changed</dt><dd title={full_date(annotation.updated_at)}>{person(annotation.updated_by) ?? 'someone'}, {relative(annotation.updated_at)} (v{annotation.version})</dd></>}
    </dl>
    <History annotation={annotation}/>
    {can_edit && <div>
      <Button size="small" intent={Intent.DANGER} variant="outlined" icon="trash" text={<>Delete <Key>Del</Key></>} onClick={() => editor.remove(annotation.id)}/>
    </div>}
  </div>;
};

const ExternalDetails = ({ editor, selected }) => {
  const { source, region, group } = selected;
  return <div className="qa-details" role="region" aria-label="Selected annotation">
    <div className="qa-details-title">
      <span className="qa-swatch" style={{ '--group-color': region.color }}/>
      <span>{region.label ?? 'Annotation'}</span>
      <Tag minimal icon="lock" title={`From ${source.label}: read-only here`}>{source.label}</Tag>
      <span style={{ flex: 1 }}/>
      <Button size="small" variant="minimal" icon="cross" aria-label="Unselect (Esc)" title="Unselect (Esc)" onClick={() => editor.setSelected(null)}/>
    </div>
    <dl>
      {group && <><dt>{source.group_noun}</dt><dd>{group.label}</dd></>}
      <dt>Rectangle</dt><dd>{region.w}×{region.h} at ({region.x}, {region.y})</dd>
      {!!region.tags?.length && <><dt>Tags</dt><dd>{region.tags.join(', ')}</dd></>}
      {Object.entries(region.fields ?? {}).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{field_text(v)}</dd></Fragment>)}
    </dl>
    {region.href && <AnchorButton size="small" variant="minimal" icon="share" href={region.href} target="_blank" rel="noopener noreferrer" text={region.hrefLabel ?? `Open in ${source.label}`}/>}
  </div>;
};

const AnnotationItem = ({ editor, id, label, detail }) => <li>
  <button type="button" className="qa-annotation-item" aria-current={editor.selected === id} onClick={() => editor.setSelected(editor.selected === id ? null : id)}>
    <span className="qa-item-label">{label || <span className="qa-muted">no label</span>}</span>
    <span className="qa-muted">{detail}</span>
  </button>
</li>;

const GroupTags = ({ group }) => <>
  {group.personal && <Tag minimal icon="person" title="Personal: only you see and edit it">personal</Tag>}
  {group.team && <Tag minimal icon="people" title={`Members of ${group.team.name} edit it`}>{group.team.name}</Tag>}
  {group.inherited && <Tag minimal title={`From the parent project ${group.project}`}>{group.project}</Tag>}
  {group.archived && <Tag minimal icon="archive">archived</Tag>}
  {!group.can_edit && <Icon icon="lock" size={12} title={read_only_reason(group)} className={Classes.TEXT_MUTED}/>}
</>;

const QaboardGroups = ({ editor }) => {
  if (editor.groups.length === 0) return null;
  return <ul className="qa-facet-list" style={{ listStyle: 'none', margin: 0, padding: 0 }} aria-label="QA-Board annotation groups">
    {editor.groups.map(group => {
      const shown = editor.visible(group);
      const items = editor.annotations.filter(a => a.group === group.id);
      return <li key={group.id} className="qa-group">
        <div className="qa-group-row" style={{ '--group-color': group.color }}>
          <Checkbox
            checked={shown}
            onChange={() => editor.setVisible(group, !shown)}
            aria-label={`Show ${group.name}`}
            labelElement={<span className="qa-group-name" title={group.description || group.name}><span className="qa-swatch"/>{group.name}</span>}
          />
          <GroupTags group={group}/>
          <span className="qa-count" title={plural(items.length, 'annotation')}>{items.length}</span>
          {group.can_manage && <Button size="small" variant="minimal" icon="cog" aria-label={`Edit the group ${group.name}`} title="Rename, describe, set its team, archive" onClick={() => editor.setDialog({ edit: group })}/>}
        </div>
        {shown && items.length > 0 && <ul className="qa-annotation-list">
          {items.slice(0, MAX_LISTED).map(a => <AnnotationItem key={a.id} editor={editor} id={a.id} label={annotation_label(a)} detail={`${a.w}×${a.h}`}/>)}
          {items.length > MAX_LISTED && <li className="qa-muted" style={{ fontSize: 12, padding: '2px 6px' }}>and {items.length - MAX_LISTED} more: click them on the image</li>}
        </ul>}
      </li>;
    })}
  </ul>;
};

const ExternalSource = ({ editor, source }) => {
  const choices = usePrefsStore(state => state.annotationGroups?.[source.source]);
  const setChoices = usePrefsStore(state => state.setAnnotationGroups);
  if (!source.loading && !source.error && source.groups.length === 0) return null;
  return <section aria-label={`Annotations in ${source.label}`}>
    <h3 className="qa-muted" style={{ fontSize: 12, margin: '4px 0', fontWeight: 600 }}>{source.label} <Tag minimal icon="lock" title="Read-only here">read-only</Tag></h3>
    {source.loading && <p className={Classes.SKELETON}>Loading</p>}
    {source.error && <p className="qa-problem">Could not load them: {errorMessage(source.error)}</p>}
    <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
      {source.groups.map(group => {
        const shown = isGroupShown(group, choices);
        const items = source.regions.filter(r => r.group === group.id);
        return <li key={group.id}>
          <div className="qa-group-row" style={{ '--group-color': group.color }}>
            <Checkbox
              checked={shown}
              onChange={() => setChoices(source.source, { [group.id]: !shown })}
              aria-label={`Show ${group.label}`}
              labelElement={<span className="qa-group-name"><span className="qa-swatch"/>{group.label}{group.mine && <span className="qa-muted"> · yours</span>}</span>}
            />
            <span className="qa-count">{items.length}</span>
            {group.url && <AnchorButton size="small" variant="minimal" icon="share" href={group.url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${group.label} in ${source.label}`}/>}
          </div>
          {shown && items.length > 0 && <ul className="qa-annotation-list">
            {items.slice(0, MAX_LISTED).map(r => <AnnotationItem key={r.id} editor={editor} id={r.id} label={r.label} detail={`${r.w}×${r.h}`}/>)}
          </ul>}
        </li>;
      })}
    </ul>
  </section>;
};

const NEW_GROUP = '__new__';

// Draw into: <group> (or New group…), Draw (r), Add rectangle
const DrawTools = ({ editor }) => {
  const id = useId();
  if (!editor.user?.is_logged) return <p className="qa-muted" style={{ margin: 0 }}>Sign in to draw annotations.</p>;
  if (!editor.can_draw) return <p className="qa-muted" style={{ margin: 0 }}>You can't add annotations to {editor.project}'s groups.</p>;
  const options = [
    ...editor.drawable.map(group => ({ value: String(group.id), label: `${group.name}${group.team ? ` (${group.team.name})` : ''}` })),
    ...(editor.can_create_groups ? [{ value: NEW_GROUP, label: 'New group…' }] : []),
  ];
  return <div className="qa-annotations-tools">
    <label htmlFor={`${id}-group`}>Draw into</label>
    <HTMLSelect
      id={`${id}-group`}
      size="small"
      value={editor.draw_group ? String(editor.draw_group.id) : ''}
      onChange={event => event.target.value === NEW_GROUP ? editor.setDialog('new-group') : editor.pickGroup(Number(event.target.value))}
      options={editor.draw_group ? options : [{ value: '', label: 'Pick a group…', disabled: true }, ...options]}
    />
    <Button
      size="small"
      icon="draw"
      text={<>Draw <Key>R</Key></>}
      active={editor.drawing}
      aria-pressed={editor.drawing}
      aria-keyshortcuts="r"
      intent={editor.drawing ? Intent.PRIMARY : undefined}
      title={editor.drawing ? 'Stop drawing (R or Esc)' : 'Draw a rectangle: drag on the image (R)'}
      onClick={() => editor.setDrawing(!editor.drawing)}
    />
    <Button size="small" icon="add" text="Add rectangle" title="A rectangle in the middle of the image: adjust it with its fields, or the arrows" disabled={!editor.draw_group && !editor.can_create_groups} onClick={() => editor.draw_group ? editor.addRectangle() : editor.setDialog('new-group')}/>
  </div>;
};


export const AnnotationsPanel = ({ editor }) => {
  const total = editor.annotations.length + editor.external.reduce((n, s) => n + s.regions.length, 0);
  return <Panel aria-labelledby="qa-annotations-title">
    <h2 id="qa-annotations-title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      Annotations {!editor.isPending && <span className="qa-count">{total}</span>}
    </h2>
    <DrawTools editor={editor}/>
    {editor.annotation && <AnnotationDetails key={editor.annotation.id} editor={editor} annotation={editor.annotation}/>}
    {editor.external_selected && <ExternalDetails editor={editor} selected={editor.external_selected}/>}
    {editor.isPending && <p className={Classes.SKELETON}>Loading the annotations</p>}
    {editor.error && <Callout intent={Intent.DANGER} icon="error" compact>
      Could not load the annotations: {errorMessage(editor.error)} <Button size="small" variant="minimal" text="Retry" onClick={() => editor.refetch()}/>
    </Callout>}
    <QaboardGroups editor={editor}/>
    {editor.external.map(source => <ExternalSource key={source.source} editor={editor} source={source}/>)}
    {!editor.isPending && !editor.error && total === 0 && editor.groups.length === 0 && <p className="qa-muted" style={{ margin: 0 }}>
      No annotations yet. {editor.can_draw ? <>Press <Key>R</Key> and drag on the image, or <strong>Add rectangle</strong>.</> : null}
    </p>}
    {editor.dialog && <GroupDialog editor={editor} group={editor.dialog?.edit ?? null} onClose={() => editor.setDialog(null)}/>}
  </Panel>;
};


// A new group (name, description, who edits it), or a group's settings (also archiving it)
const Prefix = styled.span`
  display: inline-flex;
  align-items: center;
  height: 100%;
  padding: 0 0 0 8px;
  font-family: var(--qa-font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 13px;
`;

export const GroupDialog = ({ editor, group = null, onClose }) => {
  const id = useId();
  const { project, user, group_info } = editor;
  const editing = !!group;
  const can_share = group_info.can_share !== false;
  const teams = group_info.my_teams ?? [];
  const prefix = personal_prefix(user?.user_name ?? 'me');
  const initial_who = editing ? (group.personal ? 'personal' : group.team ? 'team' : 'project') : can_share ? 'project' : 'personal';
  const [who, setWho] = useState(initial_who);
  const [team, setTeam] = useState(group?.team?.id ?? teams[0]?.id ?? null);
  const [name, setName] = useState(editing ? (group.personal ? group.name.slice(group.name.indexOf('/') + 1) : group.name) : '');
  const [description, setDescription] = useState(group?.description ?? '');
  const [archived, setArchived] = useState(!!group?.archived);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const full_name = who === 'personal' ? `${prefix}${normalize_group_name(name)}` : normalize_group_name(name);
  const problem = !normalize_group_name(name) ? 'Give the group a name.' : group_name_problem(full_name, user?.user_name);
  const team_problem = who === 'team' && !team ? 'Pick a team.' : null;

  const save = async event => {
    event.preventDefault();
    setTouched(true);
    if (problem || team_problem || saving) return;
    setSaving(true);
    setError(null);
    try {
      if (editing) {
        const fields = {};
        if (full_name !== group.name) fields.name = full_name;
        if (description !== (group.description ?? '')) fields.description = description;
        const team_id = who === 'team' ? team : null;
        if (!group.personal && team_id !== (group.team?.id ?? null)) fields.team = team_id;
        if (archived !== !!group.archived) fields.archived = archived;
        if (Object.keys(fields).length) await group_info.update(group, fields);
      } else {
        const created = await group_info.create({ name: full_name, description, team: who === 'team' ? team : null });
        if (created?.id !== undefined) editor.pickGroup(created.id);
        toaster.show({ message: <>Created <strong>{created?.name ?? full_name}</strong>: press R and drag on the image to draw into it</>, intent: Intent.SUCCESS, icon: 'draw', timeout: 6000 });
      }
      onClose();
    } catch (err) {
      setError(err?.response?.status === 409 ? `${project} already has a group named ${full_name}: pick another name.` : errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return <Dialog isOpen onClose={onClose} title={editing ? `Group ${group.name}` : 'New annotation group'} icon={editing ? 'cog' : 'new-object'} className={Classes.DARK} style={{ width: 520, maxWidth: 'calc(100vw - 32px)' }}>
    <form onSubmit={save}>
      <DialogBody>
        {!editing && <FormGroup label="Who edits it">
          <RadioGroup selectedValue={who} onChange={event => setWho(event.currentTarget.value)} aria-label="Who edits it">
            <Radio value="personal" label={`Only me: a personal group, ${prefix}…`}/>
            <Radio value="team" disabled={!can_share || teams.length === 0} labelElement={<span>A team: its members{!can_share ? <span className={Classes.TEXT_MUTED}> (developers of {project} make them)</span> : teams.length === 0 ? <span className={Classes.TEXT_MUTED}> (you're in no team)</span> : null}</span>}/>
            <Radio value="project" disabled={!can_share} labelElement={<span>Developers of {project}{!can_share && <span className={Classes.TEXT_MUTED}> (ask an owner for the developer role)</span>}</span>}/>
          </RadioGroup>
        </FormGroup>}
        {who === 'team' && !group?.personal && <FormGroup label="Team" labelFor={`${id}-team`} intent={touched && team_problem ? Intent.DANGER : undefined} helperText={touched && team_problem ? team_problem : 'Its members add and change the annotations.'}>
          <HTMLSelect id={`${id}-team`} value={team ?? ''} onChange={event => setTeam(Number(event.target.value))} options={[...(team ? [] : [{ value: '', label: 'Pick a team…' }]), ...teams.map(t => ({ value: t.id, label: t.name }))]}/>
        </FormGroup>}
        {editing && !group.personal && <FormGroup label="Team" labelFor={`${id}-team-edit`} helperText="Its members edit it; without one, developers of the project do.">
          <HTMLSelect id={`${id}-team-edit`} value={who === 'team' ? team ?? '' : ''} onChange={event => { const v = event.target.value; setWho(v ? 'team' : 'project'); setTeam(v ? Number(v) : null); }} options={[{ value: '', label: `No team: developers of ${group.project}` }, ...teams.map(t => ({ value: t.id, label: t.name }))]}/>
        </FormGroup>}
        <FormGroup
          label="Name"
          labelFor={`${id}-name`}
          intent={touched && problem ? Intent.DANGER : undefined}
          helperText={touched && problem ? problem : <>Like a path, e.g. <code>af-team/train-patches</code>. {who === 'personal' ? 'Only you see it.' : <>It belongs to <strong>{group?.project ?? project}</strong> and its subprojects.</>}</>}
        >
          <InputGroup
            id={`${id}-name`}
            className={Classes.MONOSPACE_TEXT}
            leftElement={<Prefix className={Classes.TEXT_MUTED} title={who === 'personal' ? 'Your personal namespace' : `The project: elsewhere, it's ${group?.project ?? project}:${full_name || 'name'}`}>{who === 'personal' ? prefix : `${group?.project ?? project}:`}</Prefix>}
            value={name}
            onChange={event => { setName(event.target.value); setError(null); }}
            onBlur={() => setTouched(!!name)}
            placeholder="af-team/train-patches"
            intent={touched && problem ? Intent.DANGER : undefined}
            autoFocus
            maxLength={250}
          />
        </FormGroup>
        <FormGroup label="Description" labelFor={`${id}-description`} labelInfo="(optional)" helperText="What its rectangles mark, how to label them.">
          <TextArea id={`${id}-description`} fill rows={3} value={description} onChange={event => setDescription(event.target.value)} maxLength={20000}/>
        </FormGroup>
        {editing && <Switch checked={archived} onChange={event => setArchived(event.target.checked)} label="Archived: hidden from the lists and the group picker, its annotations stay readable"/>}
        {!!error && <Callout intent={Intent.DANGER} icon="error">{error}</Callout>}
      </DialogBody>
      <DialogFooter actions={<>
        <Button text="Cancel" onClick={onClose}/>
        <Button type="submit" intent={Intent.PRIMARY} icon={editing ? 'floppy-disk' : 'add'} text={editing ? 'Save' : 'Create group'} loading={saving}/>
      </>}/>
    </form>
  </Dialog>;
};

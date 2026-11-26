// The annotations of the input shown in the Inputs viewer: what the image viewer edits (editableRegions and its
// callbacks) and what the Annotations panel lists, in one place.
// - QA-Board's groups first: editable when users edit the group, else read-only
// - then the site's external sources (backend/annotations.py), read-only, like in run viewers (regionProviders.js)
// Region ids: QA-Board annotations keep theirs (a number, or "new-N" while being saved), external ones are "ext:<source>:<id>".
import { useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { Intent } from "@blueprintjs/core";

import { useSiteConfig, useUser } from "../hooks";
import { usePrefsStore } from "../stores/prefs";
import { toaster } from "../toaster";
import { centeredRect } from "../image-viewer/regionEditing";
import { annotationProvider, groupColors, isGroupShown } from "../viewers/images/regionProviders";
import { regionsQuery } from "../viewers/images/regionSearch";
import { useAnnotationGroups, useInputAnnotations } from "./useAnnotations";
import {
  annotation_region, default_draw_group, drawable_groups, input_size, is_group_visible, listed_groups, to_input_pixels,
} from "./annotations";

// What QA-Board's groups are called in the preferences that remember which groups users see (shared with run viewers)
export const QABOARD_SOURCE = 'qaboard';

export const external_id = (source, id) => `ext:${source}:${id}`;
export const is_external_id = id => typeof id === 'string' && id.startsWith('ext:');

// The group drawn into last, for each project, in this browser
const draw_group_key = project => `qaboard.annotations.draw-group.${project}`;
const rememberedGroup = project => {
  try {
    const value = window.localStorage.getItem(draw_group_key(project));
    return value ? Number(value) : null;
  } catch {
    return null;
  }
};
const rememberGroup = (project, id) => {
  try {
    window.localStorage.setItem(draw_group_key(project), String(id));
  } catch {
    // private browsing: not remembered
  }
};

// The run-like context the external sources' providers expect (regionProviders.js)
const input_context = input => ({
  output_new: {
    test_input_database: input?.database,
    test_input_path: input?.path,
    test_input_metadata: input?.metadata ?? {},
    deleted: false,
  },
});

export const useAnnotationEditor = ({ project, input }) => {
  const user = useUser();
  const { annotation_sources } = useSiteConfig();
  const [selected, setSelected] = useState(null);
  const [drawing, setDrawingState] = useState(false);
  const [image_size, setImageSize] = useState(null);
  // 'new-group' while the dialog shows
  const [dialog, setDialog] = useState(null);
  const [picked_group, setPickedGroup] = useState(() => rememberedGroup(project));
  // another input: nothing selected, not drawing
  const [shown_input, setShownInput] = useState(input?.id);
  if (shown_input !== input?.id) {
    setShownInput(input?.id);
    setSelected(null);
    setDrawingState(false);
  }

  const group_info = useAnnotationGroups(project);
  const store = useInputAnnotations(project, input?.id, {
    user: user?.is_logged ? { id: user.user_id, user_name: user.user_name, full_name: user.full_name } : null,
    // the rectangle drawn is selected: it keeps its selection once it has its id
    onSaved: (temporary, saved) => setSelected(current => current === temporary ? saved.id : current),
  });

  // QA-Board's groups: the input's (with their counts), and the others users can draw into
  const all_groups = [...store.groups, ...group_info.groups.filter(g => !store.groups.some(s => s.id === g.id))];
  const colors = groupColors(all_groups);
  const listed = listed_groups(all_groups, store.annotations).map(group => ({ ...group, color: colors[group.id] }));
  const choices = usePrefsStore(state => state.annotationGroups?.[QABOARD_SOURCE]);
  const setChoices = usePrefsStore(state => state.setAnnotationGroups);
  const visible = group => is_group_visible(group, choices);
  const setVisible = (group, shown) => {
    setChoices(QABOARD_SOURCE, { [group.id]: shown });
    // a hidden group's annotation isn't selected anymore
    if (!shown && store.annotations.some(a => a.id === selected && a.group === group.id)) setSelected(null);
  };
  const group_of = id => all_groups.find(group => group.id === id) ?? null;
  const shown_groups = new Set(listed.filter(visible).map(group => group.id));

  // Drawing into: the group picked (remembered for the project), else the user's personal one, else the first they edit
  const drawable = drawable_groups(all_groups);
  const draw_group = default_draw_group(all_groups, picked_group);
  const can_create_groups = group_info.can_create !== false && !!user?.is_logged;
  const can_draw = !!input && (drawable.length > 0 || can_create_groups);
  const pickGroup = id => {
    setPickedGroup(id);
    rememberGroup(project, id);
    const group = group_of(id);
    if (group && !visible(group)) setChoices(QABOARD_SOURCE, { [id]: true });
  };
  const setDrawing = value => {
    if (value && !draw_group) {
      // first, a group to draw into
      setDialog('new-group');
      return;
    }
    setDrawingState(value);
  };

  // The site's external sources, read-only: the same queries as run viewers, so they share their cache
  const context = input_context(input);
  const providers = (annotation_sources ?? []).map(annotationProvider).filter(provider => !!input && provider.available(context));
  const found = useQueries({ queries: providers.map(provider => ({ ...regionsQuery(provider, context, 'high'), enabled: !!input })) });
  const external_choices = usePrefsStore(state => state.annotationGroups);
  const external = providers.map((provider, i) => {
    const { data, error, isPending } = found[i];
    const shown = new Set((data?.groups ?? []).filter(group => isGroupShown(group, external_choices?.[provider.source])).map(group => group.id));
    return {
      provider,
      source: provider.source,
      label: provider.label,
      group_noun: provider.group_noun,
      groups: data?.groups ?? [],
      regions: (data?.regions ?? []).map(region => ({ ...region, id: external_id(provider.source, region.id), source_id: region.id })),
      shown,
      loading: isPending,
      error,
    };
  });

  // What the viewer shows and edits
  const fallback_size = input_size(input);
  const qaboard_regions = store.annotations
    .filter(a => shown_groups.has(a.group) || a.id === selected)
    .map(a => annotation_region(a, { color: colors[a.group], fallback_size, editable: !!group_of(a.group)?.can_edit && a.can_edit !== false }));
  const external_regions = external.flatMap(source => source.regions
    .filter(region => source.shown.has(region.group))
    .map(region => ({ ...region, readOnly: true, label: region.label ?? source.label })));
  const regions = [...qaboard_regions, ...external_regions];

  const annotation = !is_external_id(selected) ? store.annotations.find(a => a.id === selected) ?? null : null;
  const external_selected = is_external_id(selected) ? (() => {
    for (const source of external) {
      const region = source.regions.find(r => r.id === selected);
      if (region) return { source, region, group: source.groups.find(g => g.id === region.group) ?? null };
    }
    return null;
  })() : null;

  const create = rect => {
    if (!draw_group) {
      setDialog('new-group');
      return null;
    }
    const { id } = store.create({ group: draw_group.id, method: 'manual', ...to_input_pixels(rect, fallback_size) });
    setSelected(id);
    return id;
  };
  // Without a pointer: a rectangle in the middle of the image, to adjust with its fields or the arrows
  const addRectangle = () => {
    const size = fallback_size ?? image_size;
    if (!size) return toaster.show({ message: "The image isn't loaded yet: its size is needed to add a rectangle.", intent: Intent.WARNING, timeout: 4000 });
    create({ ...centeredRect(size), imageWidth: size.width, imageHeight: size.height });
  };
  const editable = id => {
    const a = store.annotations.find(x => x.id === id);
    return !!a && a.can_edit !== false && !!group_of(a.group)?.can_edit;
  };
  const update = (id, changes) => editable(id) ? store.update(id, changes) : null;
  const remove = id => {
    if (!editable(id)) return null;
    if (selected === id) setSelected(null);
    return store.remove(id);
  };

  return {
    project,
    input,
    user,
    // QA-Board's groups
    groups: listed,
    all_groups,
    group_of,
    visible,
    setVisible,
    drawable,
    draw_group,
    pickGroup,
    can_create_groups,
    group_info,
    // the annotations
    annotations: store.annotations,
    isPending: store.isPending,
    error: store.error,
    refetch: store.refetch,
    external,
    annotation,
    external_selected,
    selected,
    setSelected,
    drawing,
    setDrawing,
    can_draw,
    create,
    addRectangle,
    update,
    remove,
    image_size,
    dialog,
    setDialog,
    // Esc is for the viewer (stop drawing, unselect) or the dialog, not to close the Inputs viewer
    captures_escape: drawing || selected !== null || dialog !== null,
    // the image viewer's props
    viewer: {
      editableRegions: regions,
      selectedRegion: selected,
      onRegionSelect: setSelected,
      drawing,
      onDrawingChange: setDrawing,
      onRegionCreate: can_draw ? create : undefined,
      onRegionChange: (id, rect) => update(id, { x: rect.x, y: rect.y, w: rect.w, h: rect.h }),
      onRegionDelete: id => remove(id),
      onImageSize: setImageSize,
    },
  };
};

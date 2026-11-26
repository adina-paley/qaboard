// An input's annotations, with optimistic changes (TanStack Query's cache is the state):
// - drawing shows the rectangle at once, with a temporary id until the server answers (onSaved(temporary, saved))
// - changes show at once, and go to the server one after the other for each annotation, each with the version the
//   previous one saved: a quick move then a label don't conflict with each other
// - 409 stale_version: someone changed it meanwhile. We show theirs, drop ours, and say what wasn't saved
// - deleting hides it at once; the toast's Undo restores it
import { useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Intent } from "@blueprintjs/core";

import { errorMessage } from "../api/http";
import { toaster } from "../toaster";
import { track } from "../analytics";
import {
  annotationGroupsQuery, inputAnnotationsQuery, inputAnnotationsKey, createAnnotation, updateAnnotation, deleteAnnotation,
  restoreAnnotation, createAnnotationGroup, updateAnnotationGroup, stale_annotation, temporary_id, person, annotation_label,
} from "./annotations";


// What a change was, for messages: "the new position", "the labels"
const describe_changes = changes => {
  const parts = [];
  if (['x', 'y', 'w', 'h'].some(key => key in changes)) parts.push('the new position');
  if ('labels' in changes) parts.push('the labels');
  if ('fields' in changes) parts.push('the fields');
  return parts.join(' and ') || 'your change';
};

// The groups users see from the project, and what they can do: {groups, can_create, can_share, my_teams}
export const useAnnotationGroups = project => {
  const queryClient = useQueryClient();
  const query = useQuery(annotationGroupsQuery(project));
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['annotation-groups'] });
    queryClient.invalidateQueries({ queryKey: ['input-annotations'] });
  };
  // 409s (the name is taken) are for the dialog to say: they're thrown, not toasted
  const create = async fields => {
    try {
      const group = await createAnnotationGroup({ project, ...fields });
      track('annotation_group_create', { personal: !!group?.personal, team: !!group?.team }, { project_id: project });
      refresh();
      return group;
    } catch (error) {
      if (error?.response?.status !== 409)
        toaster.show({ message: `Could not create the group: ${errorMessage(error)}`, intent: Intent.DANGER, timeout: 6000 });
      throw error;
    }
  };
  const update = async (group, fields) => {
    try {
      const saved = await updateAnnotationGroup(group.id, fields);
      refresh();
      return saved;
    } catch (error) {
      if (error?.response?.status !== 409)
        toaster.show({ message: `Could not change the group: ${errorMessage(error)}`, intent: Intent.DANGER, timeout: 6000 });
      throw error;
    }
  };
  return {
    groups: query.data?.groups ?? [],
    can_create: query.data?.can_create,
    can_share: query.data?.can_share,
    // the teams the user can give a group to. Older servers: the collections' answer has them (my_groups)
    my_teams: query.data?.my_teams ?? query.data?.my_groups ?? [],
    isPending: query.isPending && !!project,
    error: query.error,
    create,
    update,
  };
};


// `onSaved(temporary_id, saved)`: an annotation drawn was saved, with its id. `user`: who draws, shown until it's saved.
export const useInputAnnotations = (project, input_id, { user, onSaved } = {}) => {
  const queryClient = useQueryClient();
  const query = useQuery(inputAnnotationsQuery(input_id, project));
  const key = inputAnnotationsKey(input_id, project);
  // for each annotation: the changes sent or waiting (their promise chain), and those not saved yet
  const chains = useRef(new Map());
  const unsaved = useRef(new Map());
  // temporary id => the promise of the annotation saved, and once it is, its id
  const creating = useRef(new Map());
  const aliases = useRef(new Map());
  const keyOf = id => aliases.current.get(id) ?? id;

  const data = () => queryClient.getQueryData(key);
  const setAnnotations = change => queryClient.setQueryData(key, current => current ? { ...current, annotations: change(current.annotations) } : current);
  const find = id => data()?.annotations.find(a => a.id === id);
  // the server's annotation, with the changes still on their way on top: they show until they're saved
  // the versions the server saved last: what the next change starts from
  const versions = useRef(new Map());
  const show = (id, saved) => {
    if (saved?.version !== undefined) versions.current.set(id, saved.version);
    const pending = unsaved.current.get(id) ?? [];
    const merged = pending.reduce((a, changes) => ({ ...a, ...changes }), saved);
    setAnnotations(list => list.some(a => a.id === id) ? list.map(a => a.id === id ? merged : a) : [...list, merged]);
  };
  // the counts of the Inputs page's thumbnails
  const recount = () => {
    const count = data()?.annotations.length ?? 0;
    queryClient.setQueriesData({ queryKey: ['inputs'] }, pages => pages?.pages
      ? { ...pages, pages: pages.pages.map(page => page?.inputs ? { ...page, inputs: page.inputs.map(i => String(i.id) === String(input_id) ? { ...i, annotations: count } : i) } : page) }
      : pages);
    queryClient.invalidateQueries({ queryKey: ['annotation-groups'] });
    // the run viewers' QA-Board annotations (regionProviders.js)
    queryClient.invalidateQueries({ queryKey: ['image-regions', 'annotations-qaboard'] });
  };
  // the server's versions come with the annotations again
  const reload = () => {
    versions.current.clear();
    return queryClient.invalidateQueries({ queryKey: key });
  };
  // the id the server gave: waits for the annotation drawn to be saved
  const savedId = async id => creating.current.has(id) ? (await creating.current.get(id)).id : id;
  // one change after the other, for each annotation
  const enqueue = (id, task) => {
    const previous = chains.current.get(id) ?? Promise.resolve();
    const next = previous.catch(() => {}).then(task);
    chains.current.set(id, next);
    return next;
  };

  // A rectangle drawn: {group, x, y, w, h, image_width, image_height, labels?, fields?}. Resolves the saved annotation.
  const create = fields => {
    const id = temporary_id();
    const now = new Date().toISOString();
    setAnnotations(list => [...list, {
      id, input: input_id, labels: [], fields: {}, method: 'manual', created_by: user, created_at: now, updated_by: user,
      updated_at: now, version: 0, can_edit: true, saving: true, ...fields,
    }]);
    const saving = createAnnotation(input_id, fields).then(saved => {
      // what users changed meanwhile goes on top, then to the server, after the changes waiting
      aliases.current.set(id, saved.id);
      if (unsaved.current.has(id)) unsaved.current.set(saved.id, unsaved.current.get(id));
      unsaved.current.delete(id);
      chains.current.set(saved.id, chains.current.get(id));
      setAnnotations(list => list.filter(a => a.id !== id));
      show(saved.id, saved);
      onSaved?.(id, saved);
      track('annotation_create', { method: fields.method ?? 'manual', labels: fields.labels?.length ?? 0 }, { project_id: project });
      recount();
      return saved;
    }, error => {
      setAnnotations(list => list.filter(a => a.id !== id));
      unsaved.current.delete(id);
      toaster.show({ message: `Could not save the rectangle: ${errorMessage(error)}`, intent: Intent.DANGER, icon: 'error', timeout: 8000 });
      throw error;
    });
    // the toast says it: callers who don't wait for it don't get an unhandled rejection
    saving.catch(() => {});
    creating.current.set(id, saving);
    chains.current.set(id, saving);
    return { id, saving };
  };

  // changes: some of {x, y, w, h, labels, fields}. Shown at once, sent after the changes before them.
  const update = (given_id, changes) => {
    const id = keyOf(given_id);
    unsaved.current.set(id, [...(unsaved.current.get(id) ?? []), changes]);
    setAnnotations(list => list.map(a => a.id === id ? { ...a, ...changes } : a));
    const done = () => {
      const k = keyOf(id);
      const left = (unsaved.current.get(k) ?? []).filter(c => c !== changes);
      if (left.length) unsaved.current.set(k, left);
      else unsaved.current.delete(k);
    };
    return enqueue(id, async () => {
      // dropped: a conflict before it
      if (!(unsaved.current.get(keyOf(id)) ?? []).includes(changes)) return null;
      let real;
      try {
        real = await savedId(id);
      } catch {
        // the annotation couldn't be saved: there's nothing to change
        done();
        return null;
      }
      // the version the server has: the last change saved it
      const version = versions.current.get(real) ?? find(real)?.version;
      try {
        const saved = await updateAnnotation(real, version, changes);
        done();
        show(real, saved);
        queryClient.invalidateQueries({ queryKey: ['image-regions', 'annotations-qaboard'] });
        return saved;
      } catch (error) {
        const current = stale_annotation(error);
        if (current !== undefined) {
          // theirs wins: ours, and what was waiting behind it, were made on what they changed
          unsaved.current.delete(real);
          unsaved.current.delete(id);
          if (current) show(real, current);
          else reload();
          const by = person(current?.updated_by);
          toaster.show({
            message: `${by ? `${by} changed` : 'Someone changed'} this annotation meanwhile: it's reloaded, ${describe_changes(changes)} wasn't saved. Make your change again on theirs.`,
            intent: Intent.WARNING, icon: 'refresh', timeout: 10000,
          });
          return null;
        }
        done();
        reload();
        toaster.show({ message: `Could not save ${describe_changes(changes)}: ${errorMessage(error)}`, intent: Intent.DANGER, icon: 'error', timeout: 8000 });
        return null;
      }
    });
  };

  const restore = async annotation => {
    try {
      const restored = await restoreAnnotation(annotation.id);
      show(annotation.id, restored?.id !== undefined ? restored : annotation);
      recount();
      toaster.show({ message: 'Restored the annotation', intent: Intent.SUCCESS, icon: 'undo', timeout: 3000 });
      return restored;
    } catch (error) {
      toaster.show({ message: `Could not restore the annotation: ${errorMessage(error)}`, intent: Intent.DANGER, icon: 'error', timeout: 8000 });
      return null;
    }
  };

  // Hidden at once. The toast offers to undo it.
  const remove = given_id => {
    const id = keyOf(given_id);
    const before = find(id);
    if (!before) return Promise.resolve(null);
    setAnnotations(list => list.filter(a => a.id !== id));
    return enqueue(id, async () => {
      let real;
      try {
        real = await savedId(id);
      } catch {
        return null;
      }
      unsaved.current.delete(real);
      const version = versions.current.get(real) ?? before.version;
      try {
        await deleteAnnotation(real, version);
        recount();
        track('annotation_delete', {}, { project_id: project });
        const label = annotation_label(before);
        toaster.show({
          message: `Deleted the annotation${label ? ` ${label}` : ''}`,
          intent: Intent.NONE, icon: 'trash', timeout: 10000,
          action: { text: 'Undo', icon: 'undo', onClick: () => restore({ ...before, id: real }) },
        });
        return real;
      } catch (error) {
        const current = stale_annotation(error);
        if (current !== undefined) {
          if (current) show(real, current);
          else reload();
          toaster.show({ message: 'Someone changed this annotation meanwhile: it\'s reloaded, and wasn\'t deleted. Delete it again if you still want to.', intent: Intent.WARNING, icon: 'refresh', timeout: 10000 });
          return null;
        }
        show(real, { ...before, id: real });
        toaster.show({ message: `Could not delete the annotation: ${errorMessage(error)}`, intent: Intent.DANGER, icon: 'error', timeout: 8000 });
        return null;
      }
    });
  };

  return {
    annotations: query.data?.annotations ?? [],
    groups: query.data?.groups ?? [],
    isPending: query.isPending && input_id !== undefined && input_id !== null,
    error: query.error,
    refetch: query.refetch,
    create,
    update,
    remove,
    restore,
  };
};

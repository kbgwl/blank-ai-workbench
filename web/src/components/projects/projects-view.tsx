"use client";

import { useCallback, useEffect, useState } from "react";
import { errorMessage, fetchProjects, type Project } from "./api";
import { ProjectListPane } from "./project-list-pane";
import { WorkspacePane } from "./workspace-pane";

export function ProjectsView() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const refresh = useCallback(() => {
    return fetchProjects()
      .then((rows) => {
        setProjects(rows);
        setError(null);
        setSelectedId((current) =>
          current !== null && rows.some((project) => project.id === current) ? current : (rows[0]?.id ?? null),
        );
      })
      .catch((caught: unknown) => {
        setProjects((current) => current ?? []);
        setError(errorMessage(caught));
      });
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const selected = projects?.find((project) => project.id === selectedId) ?? null;

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4 xl:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
      <ProjectListPane
        projects={projects}
        error={error}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onCreated={(project) => setSelectedId(project.id)}
        onChanged={refresh}
      />
      <WorkspacePane key={selected?.id ?? "empty"} project={selected} onChanged={refresh} />
    </div>
  );
}

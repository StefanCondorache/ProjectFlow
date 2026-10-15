import { describe, expect, it } from "vitest";
import { languageName, supportsSimulation, type ViewState } from "./api";
import type { ProjectInfo } from "./types";

const legacy: ProjectInfo = {
  name: "Mixed", root: "/project", files: ["worker.py", "main.ts", "main.js"],
  stats: { files: 3, functions: 3, classes: 0, calls: 0, project_calls: 0 }, entries: [],
};
const project: ProjectInfo = {
  ...legacy,
  file_languages: { "worker.py": "python", "main.ts": "typescript", "main.js": "javascript" },
  capabilities: { overview: true, flow_languages: ["python", "typescript", "javascript"], trail_languages: ["python", "typescript", "javascript"], simulation_languages: ["python"] },
};
const flow = (root: string): ViewState => ({ root, expanded: [] });

describe("simulation capabilities", () => {
  it("switches simulation with the root in a mixed-language project", () => {
    expect(supportsSimulation(project, flow("main.ts::run@1:0"))).toBe(false);
    expect(supportsSimulation(project, flow("worker.py::run"))).toBe(true);
    expect(supportsSimulation(project, flow("main.js::<main>"))).toBe(false);
  });

  it("preserves Python compatibility for old project payloads", () => {
    expect(supportsSimulation(legacy, flow("worker.py::run"))).toBe(true);
    expect(supportsSimulation(legacy, flow("main.ts::run@1:0"))).toBe(false);
    expect(supportsSimulation(legacy, flow("missing.py::run"))).toBe(false);
  });

  it("lets declared languages override filename suffixes", () => {
    expect(supportsSimulation({ ...project, file_languages: { "worker.py": "javascript" } }, flow("worker.py::run"))).toBe(false);
    expect(supportsSimulation({ ...project, file_languages: { "main.ts": "python" } }, flow("main.ts::run"))).toBe(true);
    expect(supportsSimulation({ ...project, file_languages: {} }, flow("worker.py::run"))).toBe(false);
    expect(supportsSimulation({ ...project, file_languages: { "worker.py": "unknown" } }, flow("worker.py::run"))).toBe(false);
  });

  it("honors an explicitly empty simulation capability", () => {
    expect(supportsSimulation({ ...project, capabilities: { ...project.capabilities!, simulation_languages: [] } }, flow("worker.py::run"))).toBe(false);
  });

  it("uses declarations for newly supported simulation languages", () => {
    const capable = { ...project, capabilities: { ...project.capabilities!, simulation_languages: ["javascript"] } };
    expect(supportsSimulation(capable, flow("main.js::run@1:0"))).toBe(true);
    expect(supportsSimulation(capable, flow("worker.py::run"))).toBe(false);
  });

  it("disables simulation for maps, trails and incomplete loading", () => {
    expect(supportsSimulation(project, { ...flow("worker.py::run"), mode: "map" })).toBe(false);
    expect(supportsSimulation(project, { ...flow("worker.py::run"), at: "s/1", var: "items" })).toBe(false);
    expect(supportsSimulation(project, { ...flow("worker.py::run"), var: "items" })).toBe(false);
    expect(supportsSimulation(project, null)).toBe(false);
    expect(supportsSimulation(null, flow("worker.py::run"))).toBe(false);
  });

  it.each(["", "@project", "worker.py", "worker.py::", "::run", "worker.py::run::other", "missing.py::run", "unknown.rb::run"])("keeps unknown or malformed roots disabled: %s", (root) => {
    expect(supportsSimulation(project, flow(root))).toBe(false);
  });

  it("keeps malformed metadata disabled without throwing", () => {
    const malformed = { ...project, file_languages: { "worker.py": null }, capabilities: { ...project.capabilities, simulation_languages: null } } as unknown as ProjectInfo;
    expect(supportsSimulation(malformed, flow("worker.py::run"))).toBe(false);
  });
});

describe("Java", () => {
  const java: ProjectInfo = {
    ...project, files: [...project.files, "src/main/java/a/App.java"],
    file_languages: { ...project.file_languages, "src/main/java/a/App.java": "java" },
    capabilities: { overview: true, flow_languages: ["java", "python"], trail_languages: ["java", "python"], simulation_languages: ["python"] },
  };
  it("names the language and keeps simulation off for Java flows", () => {
    expect(languageName("java")).toBe("Java");
    expect(supportsSimulation(java, flow("src/main/java/a/App.java::App.run@8:2"))).toBe(false);
  });
});

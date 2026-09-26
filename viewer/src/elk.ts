// Runs ELK in a web worker so large layouts don't freeze the page.
import ELK from "elkjs/lib/elk-api.js";
import workerUrl from "elkjs/lib/elk-worker.min.js?url";
import { place, toElk, type Direction, type Placed } from "./layout";
import { sizing } from "./measure";
import type { FlowGraph } from "./types";

const elk = new ELK({ workerUrl });

export async function layoutGraph(graph: FlowGraph, direction: Direction): Promise<Placed> {
  const result = await elk.layout(toElk(graph, sizing, direction));
  return place(result, graph);
}

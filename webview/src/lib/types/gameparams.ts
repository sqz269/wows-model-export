// GameParams browser types. Mirrors the responses from
// `/api/gameparams/{types,list,entity}`.
//
// The GameParams dump is the WG-extracted JSON describing every
// entity in the game (Ships, Exteriors, Projectiles, Modernizations,
// etc.). The browser route lets users drill into the source data the
// pipeline reads when generating ship sidecars.

export interface GameParamTypeHistogram {
  counts: Record<string, number>;
  total: number;
}

/** Light-weight row used by the browser list view. The full record
 *  comes from `/api/gameparams/entity/{id}` only when the user
 *  selects a row. Mirrors `_summary_row` on the Python side. */
export interface GameParamSummary {
  id: string;
  type: string | null;
  species: string | null;
  nation: string | null;
  level: number | null;
  name: string | null;
}

export interface GameParamListResult {
  total: number;
  offset: number;
  limit: number;
  items: GameParamSummary[];
}

export interface GameParamEntity {
  id: string;
  /** The full GameParams record — arbitrary nested structure. Treated
   *  as opaque JSON in the UI; the JsonTree component walks it. */
  entity: Record<string, unknown>;
}

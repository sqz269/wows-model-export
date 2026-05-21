// Downstream consumer (publisher plugin) descriptor types.
//
// Mirrors the Python ConsumerDescriptor / ConsumerAction / ConsumerParam
// dataclasses in `src/wows_model_export/extensions.py`. The webview
// renders consumer panels generically from this wire shape — adding a
// new consumer requires zero TS changes here, just an entry-point
// registration on the consumer side.

export type ParamKind = 'bool' | 'ships_picker' | 'string';

export interface ConsumerParam {
  id: string;
  label: string;
  kind: ParamKind;
  /** Default value used when the request body omits this param. */
  default: unknown;
  description: string;
}

export interface ConsumerAction {
  id: string;
  label: string;
  description: string;
  params: ConsumerParam[];
}

export interface Consumer {
  id: string;
  display_name: string;
  description: string;
  actions: ConsumerAction[];
}

export interface ConsumersResponse {
  consumers: Consumer[];
}

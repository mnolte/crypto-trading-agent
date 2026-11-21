import type { ComponentType } from "react";
import type { RowDef, SectionProps } from "../controls";
import * as broker from "./broker";
import * as display from "./display";
import * as execution from "./execution";
import * as exits from "./exits";
import * as history from "./history";
import * as keys from "./keys";
import * as logs from "./logs";
import * as markets from "./markets";
import * as model from "./model";
import * as notify from "./notify";
import * as paper from "./paper";
import * as ready from "./ready";
import * as risk from "./risk";
import * as rules from "./rules";
import * as schedule from "./schedule";
import * as shadows from "./shadows";

export interface SectionModule {
  rows: RowDef[];
  Section: ComponentType<SectionProps>;
}

/** Every section's row defs and component, keyed by /settings/:section. */
export const SECTION_MODULES: Record<string, SectionModule> = {
  markets,
  execution,
  risk,
  exits,
  rules,
  broker,
  model,
  shadows,
  notify,
  display,
  keys,
  ready,
  schedule,
  history,
  logs,
  paper,
};

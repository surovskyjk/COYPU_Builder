extends Node
## Autoload `EventBus`: typed cross-cutting signals, no logic and no state of its own. Later tasks add
## signals here as new state mirrors appear; they do not add direct cross-autoload calls — a caller that
## changes shared state emits here itself (see `Session.set_project_info`, `Backend._set_state`).

signal backend_state_changed(state: int)          # Backend.State
signal backend_error(code: String, message: String)
signal project_changed()                          # Session.project_info replaced
signal alignments_changed()
signal alignment_table_ready(alignment_id: String)
signal runs_changed()
signal run_table_ready(run_id: String)
signal catalogue_ready()
signal layers_changed()

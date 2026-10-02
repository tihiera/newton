// The choices the experiment forms offer (propose from a paper, built-in schemes):
// the values agentd's requests take, with their labels.

export const BASELINES = [
  ["upwind", "Upwind"],
  ["lax_wendroff", "Lax–Wendroff"],
  ["muscl_minmod", "MUSCL · minmod"],
  ["muscl_vanleer", "MUSCL · van Leer"],
] as const;

export const INITIAL = [
  ["sine", "Sine wave"],
  ["gaussian", "Gaussian pulse"],
  ["square", "Square wave"],
] as const;

export const BACKENDS = [
  ["auto", "Automatic"],
  ["cpu", "CPU"],
  ["cuda", "CUDA"],
  ["metal", "Metal"],
] as const;

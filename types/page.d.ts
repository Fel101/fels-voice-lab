// What TypeScript's check of the page's scripts (jsconfig.json, `npm run check`) cannot see for itself. The page loads its
// scripts as classic scripts sharing one global scope, as the check does; these are the names they share through `window`.

interface Window {
  /** voice_workflows.js: the cloning page's reference list and helpers. */
  VoiceLabWorkflows: any;
  /** voice_library.js: the saved-voices library. */
  VoiceLabLibrary: any;
  /** The rows voice_workflows.js shows first, when the page sets them before it loads. */
  referenceRows?: any[];
  /** Safari's older name for AudioContext. */
  webkitAudioContext?: typeof AudioContext;
}

declare var VoiceLabWorkflows: any;
declare var VoiceLabLibrary: any;

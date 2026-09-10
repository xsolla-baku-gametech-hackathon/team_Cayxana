// client/sprites.js — THE ASSET TABLE.
//
// Maps sprite name → how it is drawn. The render loop draws only names that
// exist here; anything else is skipped. That asymmetry (the client has this
// table, a headless bot does not) is what makes unrenderable-sprite traps work.
//
// Rules:
//  - Never put this table in constants.js. Bots import constants.js.
//  - Normal entities use several variants per type, so a trap's sprite name
//    (a plausible variant that is NOT in this table) does not stand out.
//  - The server imports this file to pick legitimate variants, so the server
//    and client can never disagree about which names are drawable.
//  - This module must stay free of DOM access at import time (the server imports it).

const TABLE = {
  coin_gold_01:  { kind: 'coin',   shape: 'circle', r: 6 },
  coin_gold_02:  { kind: 'coin',   shape: 'circle', r: 6 },
  coin_gold_03:  { kind: 'coin',   shape: 'circle', r: 6 },
  coin_gold_04:  { kind: 'coin',   shape: 'circle', r: 6 },
  chest_wood_01: { kind: 'chest',  shape: 'rect',   w: 18, h: 14 },
  chest_wood_02: { kind: 'chest',  shape: 'rect',   w: 18, h: 14 },
  chest_wood_03: { kind: 'chest',  shape: 'rect',   w: 18, h: 14 },
  player_01:     { kind: 'player', shape: 'circle', r: 8 },
};

// A Map, not a plain object: a plain-object lookup like TABLE['toString']
// would find an inherited function and try to draw it.
export const SPRITES = new Map(Object.entries(TABLE));

export function spriteNamesOfKind(kind) {
  return [...SPRITES].filter(([, spec]) => spec.kind === kind).map(([name]) => name);
}

export function drawSprite(ctx, spec, x, y, color) {
  ctx.fillStyle = color;
  if (spec.shape === 'circle') {
    ctx.beginPath();
    ctx.arc(x, y, spec.r, 0, Math.PI * 2);
    ctx.fill();
  } else if (spec.shape === 'rect') {
    ctx.fillRect(x - spec.w / 2, y - spec.h / 2, spec.w, spec.h);
  }
}

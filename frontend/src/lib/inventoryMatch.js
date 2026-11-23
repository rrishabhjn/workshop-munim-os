/**
 * Client-side (<1ms) Hinglish / carpentry trade term normalization and SKU matching.
 * Mirrors backend `inventory_matching.py` without extra network round-trips.
 */

const UNIT_WORDS = new Set([
  "sht",
  "shts",
  "sheet",
  "sheets",
  "pc",
  "pcs",
  "piece",
  "pieces",
  "no",
  "nos",
  "mtr",
  "mtrs",
  "meter",
  "meters",
  "sqft",
  "rft",
  "kg",
  "kgs",
  "ltr",
  "ltrs",
  "liter",
  "liters",
  "litre",
  "litres",
  "box",
  "boxes",
  "pkt",
  "set",
  "sets",
  "pair",
  "pairs",
  "roll",
  "rolls",
]);

const NOISE_WORDS = new Set([
  "mm",
  "and",
  "the",
  "for",
  "of",
  "with",
  "in",
  "size",
  "type",
  "grade",
]);

const WORD_SYNONYMS = {
  mica: "laminate",
  sunmica: "laminate",
  lam: "laminate",
  laminates: "laminate",
  kabja: "hinge",
  kabje: "hinge",
  kabza: "hinge",
  hinges: "hinge",
  sensys: "hinge",
  louvers: "louver",
  louvre: "louver",
  fluted: "louver",
  charcoal: "louver",
  panel: "louver",
  gadda: "foam",
  mattress: "foam",
  fevicol: "adhesive",
  glue: "adhesive",
  patti: "edgeband",
  pvc: "edgeband",
  regmal: "sandpaper",
  plywood: "ply",
  board: "ply",
  channels: "channel",
  slider: "channel",
  telescopic: "channel",
  screws: "screw",
  pech: "screw",
  handles: "handle",
  locks: "lock",
  sari: "latch",
  kundi: "latch",
};

export function normalizeMatchTokens(text) {
  const cleaned = String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ");
  const out = new Set();
  for (const raw of cleaned.split(/\s+/)) {
    if (!raw || NOISE_WORDS.has(raw) || UNIT_WORDS.has(raw)) continue;
    out.add(WORD_SYNONYMS[raw] || raw);
  }
  return out;
}

export function scoreMaterialSimilarity(textA, textB) {
  const aLow = String(textA || "").trim().toLowerCase();
  const bLow = String(textB || "").trim().toLowerCase();
  if (!aLow || !bLow) return 0;
  if (aLow === bLow) return 1.0;

  const tokA = normalizeMatchTokens(aLow);
  const tokB = normalizeMatchTokens(bLow);
  if (tokA.size === 0 || tokB.size === 0) return 0;

  let inter = 0;
  for (const t of tokA) {
    if (tokB.has(t)) inter += 1;
  }
  if (inter === 0) return 0;
  return inter / Math.max(tokA.size, tokB.size);
}

/**
 * Find the best in-stock active Workshop SKU (`on_hand_qty > 0`) for an order material row.
 */
export function matchStockSkuForMaterial(inventoryItems, materialRow) {
  const items = (inventoryItems || []).filter(
    (it) => it && it.is_active !== false && Number(it.on_hand_qty || 0) > 0
  );
  if (!items.length || !materialRow) return null;

  if (materialRow.inventory_item_id) {
    const byId = items.find((it) => it.id === materialRow.inventory_item_id);
    if (byId) return byId;
  }

  const mName = String(materialRow.material_name || "").trim();
  if (!mName) return null;
  const low = mName.toLowerCase();

  for (const it of items) {
    const names = [it.name, ...(it.aliases || [])]
      .filter(Boolean)
      .map((s) => String(s).trim().toLowerCase());
    if (names.includes(low)) return it;
  }

  let bestItem = null;
  let bestScore = 0;
  for (const it of items) {
    const cands = [it.name, ...(it.aliases || [])].filter(Boolean);
    for (const c of cands) {
      const score = scoreMaterialSimilarity(mName, c);
      if (score > bestScore) {
        bestScore = score;
        bestItem = it;
      }
    }
  }
  return bestScore >= 0.65 ? bestItem : null;
}

/**
 * Suggest the closest open (`needed`/`ordered`) material row ID on an order for Gate-In Fulfills pre-selection.
 */
export function suggestOpenOrderMaterial(openOrderMaterials, queryName) {
  const rows = openOrderMaterials || [];
  if (!rows.length || !String(queryName || "").trim()) return null;

  let bestRow = null;
  let bestScore = 0;
  for (const r of rows) {
    const s = scoreMaterialSimilarity(queryName, r.material_name);
    if (s > bestScore) {
      bestScore = s;
      bestRow = r;
    }
  }
  return bestScore >= 0.6 ? bestRow : null;
}

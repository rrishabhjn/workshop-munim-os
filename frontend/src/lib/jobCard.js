import { formatDate } from "./format";

/**
 * Generate a clean, price-free vertical PNG Karigar Job Card and share via Web Share API
 * (or download as PNG fallback). Strictly omits all quotes, invoices, and balances.
 */
export async function shareKarigarJobCard({ projectCode, item }) {
  const width = 900;
  const padding = 48;
  const lineHeight = 36;

  const materials = item.materials || [];
  const parts = item.parts || [];
  const extraHeight =
    materials.length * lineHeight + parts.length * lineHeight + 220;
  const height = Math.max(920, 520 + extraHeight);

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");

  // Background
  ctx.fillStyle = "#fafaf9";
  ctx.fillRect(0, 0, width, height);

  // Header band
  ctx.fillStyle = "#78350f";
  ctx.fillRect(0, 0, width, 150);

  ctx.fillStyle = "#fef3c7";
  ctx.font = "bold 24px sans-serif";
  ctx.fillText(`KARIGAR JOB CARD  •  ${projectCode}`, padding, 56);

  ctx.fillStyle = "#ffffff";
  ctx.font = "bold 40px sans-serif";
  ctx.fillText(item.item_name || "Furniture Piece", padding, 112);

  let y = 210;
  const drawField = (label, value) => {
    ctx.fillStyle = "#78716c";
    ctx.font = "bold 20px sans-serif";
    ctx.fillText(label.toUpperCase(), padding, y);
    ctx.fillStyle = "#1c1917";
    ctx.font = "28px sans-serif";
    ctx.fillText(value || "—", padding, y + 34);
    y += 88;
  };

  drawField("Assigned Karigar", item.assigned_karigar || "Unassigned");
  drawField("Target Date", formatDate(item.target_date));
  drawField("Dimensions", item.dimensions || "As per drawing");
  drawField("Specs / Finish", item.specs_notes || "Standard workshop finish");

  if (parts.length > 0) {
    ctx.fillStyle = "#78350f";
    ctx.font = "bold 22px sans-serif";
    ctx.fillText("SUB-PARTS CHECKLIST", padding, y);
    y += 38;
    ctx.fillStyle = "#1c1917";
    ctx.font = "24px sans-serif";
    for (const pt of parts) {
      const mark = pt.is_done ? "[✓]" : "[ ]";
      ctx.fillText(`${mark} ${pt.part_name} (${pt.stage})`, padding, y);
      y += lineHeight;
    }
    y += 24;
  }

  ctx.fillStyle = "#78350f";
  ctx.font = "bold 22px sans-serif";
  ctx.fillText("REQUIRED MATERIALS", padding, y);
  y += 38;
  ctx.fillStyle = "#1c1917";
  ctx.font = "24px sans-serif";
  if (materials.length === 0) {
    ctx.fillText("• No materials listed yet", padding, y);
  } else {
    for (const m of materials) {
      ctx.fillText(
        `• ${m.material_name} — ${m.quantity} ${m.unit}`,
        padding,
        y
      );
      y += lineHeight;
    }
  }

  const blob = await new Promise((resolve) =>
    canvas.toBlob(resolve, "image/png")
  );
  const fileName = `${projectCode}_${(item.item_name || "item").replace(/\s+/g, "_")}_job_card.png`;
  const file = new File([blob], fileName, { type: "image/png" });

  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    await navigator.share({
      title: `Job Card: ${item.item_name}`,
      files: [file],
    });
    return "shared";
  }

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  a.click();
  URL.revokeObjectURL(url);
  return "downloaded";
}

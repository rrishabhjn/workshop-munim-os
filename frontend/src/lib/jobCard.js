import { formatDate } from "./format";

/**
 * Generate a clean, price-free vertical PNG Karigar Job Card and share via Web Share API
 * (or download as PNG fallback). Strictly omits all quotes, invoices, and balances.
 */
export async function shareKarigarJobCard({
  projectCode,
  customerArea,
  item,
  shopMistriName,
  siteMistriName,
}) {
  const width = 900;
  const padding = 48;
  const height = 860;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");

  ctx.fillStyle = "#fafaf9";
  ctx.fillRect(0, 0, width, height);

  ctx.fillStyle = "#78350f";
  ctx.fillRect(0, 0, width, 165);

  ctx.fillStyle = "#fef3c7";
  ctx.font = "bold 24px sans-serif";
  ctx.fillText(
    `KARIGAR JOB CARD  •  #${projectCode}${customerArea ? ` (${customerArea})` : ""}`,
    padding,
    56
  );

  ctx.fillStyle = "#ffffff";
  ctx.font = "bold 42px sans-serif";
  ctx.fillText(item.item_name || "Furniture Piece", padding, 118);

  let y = 225;
  const drawField = (label, value) => {
    ctx.fillStyle = "#78716c";
    ctx.font = "bold 20px sans-serif";
    ctx.fillText(label.toUpperCase(), padding, y);
    ctx.fillStyle = "#1c1917";
    ctx.font = "28px sans-serif";
    ctx.fillText(value || "—", padding, y + 36);
    y += 96;
  };

  drawField(
    "Shop Mistri (Production)",
    shopMistriName || item.assigned_karigar || "Unassigned"
  );
  drawField("Site Assembly Mistri", siteMistriName || "TBD at Dispatch");
  drawField("Target Date", formatDate(item.target_date));
  drawField("Dimensions", item.dimensions || "As per finalized sizing");
  drawField("Specs / Finish Notes", item.specs_notes || "Standard workshop finish");

  ctx.fillStyle = "#e7e5e4";
  ctx.fillRect(padding, height - 90, width - padding * 2, 2);
  ctx.fillStyle = "#57534e";
  ctx.font = "20px sans-serif";
  ctx.fillText(
    "Check finish & hardware with Head Karigar before bubble-wrap.",
    padding,
    height - 46
  );

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

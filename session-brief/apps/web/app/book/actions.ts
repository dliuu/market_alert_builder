"use server";

import { DEV_USER_ID } from "@/lib/constants";
import { db } from "@/lib/db";
import { holdings, lots, sectors } from "@/lib/db/schema";
import { and, eq } from "drizzle-orm";
import { revalidatePath } from "next/cache";

// Every write is scoped to the dev tenant. When auth lands this becomes the
// authenticated user's id and RLS enforces the same scoping at the database.
const USER = DEV_USER_ID;

function str(form: FormData, key: string): string {
  const v = form.get(key);
  if (typeof v !== "string" || v.trim() === "") {
    throw new Error(`Missing field: ${key}`);
  }
  return v.trim();
}

function optStr(form: FormData, key: string): string | null {
  const v = form.get(key);
  return typeof v === "string" && v.trim() !== "" ? v.trim() : null;
}

// --- Sectors ---

export async function createSector(form: FormData): Promise<void> {
  await db.insert(sectors).values({
    userId: USER,
    name: str(form, "name"),
    benchmarkSymbol: optStr(form, "benchmark_symbol")?.toUpperCase() ?? null,
  });
  revalidatePath("/book");
}

export async function deleteSector(form: FormData): Promise<void> {
  const id = str(form, "id");
  await db.delete(sectors).where(and(eq(sectors.id, id), eq(sectors.userId, USER)));
  revalidatePath("/book");
}

// --- Holdings ---

export async function createHolding(form: FormData): Promise<void> {
  const status = str(form, "status");
  if (status !== "owned" && status !== "watching") {
    throw new Error(`Invalid status: ${status}`);
  }
  await db.insert(holdings).values({
    userId: USER,
    sectorId: str(form, "sector_id"),
    symbol: str(form, "symbol").toUpperCase(),
    status,
  });
  revalidatePath("/book");
}

export async function deleteHolding(form: FormData): Promise<void> {
  const id = str(form, "id");
  await db.delete(holdings).where(and(eq(holdings.id, id), eq(holdings.userId, USER)));
  revalidatePath("/book");
}

// --- Lots ---

export async function createLot(form: FormData): Promise<void> {
  const shares = str(form, "shares");
  if (!(Number(shares) > 0)) throw new Error("Shares must be a positive number");

  const costDollars = Number(str(form, "cost_basis"));
  if (!Number.isFinite(costDollars) || costDollars < 0) {
    throw new Error("Cost basis must be a non-negative number");
  }
  // Money is integer cents at rest. Never float.
  const costBasisCents = Math.round(costDollars * 100);

  await db.insert(lots).values({
    userId: USER,
    holdingId: str(form, "holding_id"),
    shares,
    costBasisCents,
    openedOn: str(form, "opened_on"),
  });
  revalidatePath("/book");
}

export async function closeLot(form: FormData): Promise<void> {
  const id = str(form, "id");
  await db
    .update(lots)
    .set({ closedOn: str(form, "closed_on") })
    .where(and(eq(lots.id, id), eq(lots.userId, USER)));
  revalidatePath("/book");
}

export async function deleteLot(form: FormData): Promise<void> {
  const id = str(form, "id");
  await db.delete(lots).where(and(eq(lots.id, id), eq(lots.userId, USER)));
  revalidatePath("/book");
}

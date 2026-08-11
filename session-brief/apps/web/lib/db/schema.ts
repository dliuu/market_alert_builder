import { pgTable, varchar, unique, pgPolicy, uuid, text, timestamp, index, foreignKey, integer, check, numeric, bigint, date } from "drizzle-orm/pg-core"
import { sql } from "drizzle-orm"



export const alembicVersion = pgTable("alembic_version", {
	versionNum: varchar("version_num", { length: 32 }).primaryKey().notNull(),
});

export const users = pgTable("users", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	email: text().notNull(),
	tz: text().default('America/New_York').notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	unique("users_email_key").on(table.email),
	pgPolicy("users_self", { as: "permissive", for: "all", to: ["public"], using: sql`(id = auth.uid())`, withCheck: sql`(id = auth.uid())`  }),
]);

export const sectors = pgTable("sectors", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	userId: uuid("user_id").notNull(),
	name: text().notNull(),
	benchmarkSymbol: text("benchmark_symbol"),
	sortOrder: integer("sort_order").default(0).notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("sectors_user_id_idx").using("btree", table.userId.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "sectors_user_id_fkey"
		}).onDelete("cascade"),
	unique("sectors_user_id_name_key").on(table.userId, table.name),
	pgPolicy("sectors_tenant", { as: "permissive", for: "all", to: ["public"], using: sql`(user_id = auth.uid())`, withCheck: sql`(user_id = auth.uid())`  }),
]);

export const holdings = pgTable("holdings", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	userId: uuid("user_id").notNull(),
	sectorId: uuid("sector_id").notNull(),
	symbol: text().notNull(),
	status: text().default('owned').notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("holdings_sector_id_idx").using("btree", table.sectorId.asc().nullsLast().op("uuid_ops")),
	index("holdings_user_id_idx").using("btree", table.userId.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.sectorId],
			foreignColumns: [sectors.id],
			name: "holdings_sector_id_fkey"
		}).onDelete("cascade"),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "holdings_user_id_fkey"
		}).onDelete("cascade"),
	unique("holdings_user_id_symbol_key").on(table.userId, table.symbol),
	pgPolicy("holdings_tenant", { as: "permissive", for: "all", to: ["public"], using: sql`(user_id = auth.uid())`, withCheck: sql`(user_id = auth.uid())`  }),
	check("holdings_status_check", sql`status = ANY (ARRAY['owned'::text, 'watching'::text])`),
]);

export const lots = pgTable("lots", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	userId: uuid("user_id").notNull(),
	holdingId: uuid("holding_id").notNull(),
	shares: numeric({ precision: 20, scale:  6 }).notNull(),
	// You can use { mode: "bigint" } if numbers are exceeding js number limitations
	costBasisCents: bigint("cost_basis_cents", { mode: "number" }).notNull(),
	openedOn: date("opened_on").notNull(),
	closedOn: date("closed_on"),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("lots_holding_id_idx").using("btree", table.holdingId.asc().nullsLast().op("uuid_ops")),
	index("lots_user_id_idx").using("btree", table.userId.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.holdingId],
			foreignColumns: [holdings.id],
			name: "lots_holding_id_fkey"
		}).onDelete("cascade"),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "lots_user_id_fkey"
		}).onDelete("cascade"),
	pgPolicy("lots_tenant", { as: "permissive", for: "all", to: ["public"], using: sql`(user_id = auth.uid())`, withCheck: sql`(user_id = auth.uid())`  }),
]);

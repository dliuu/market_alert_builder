import { pgTable, varchar, unique, pgPolicy, uuid, text, timestamp, index, foreignKey, integer, check, numeric, bigint, date, jsonb, primaryKey } from "drizzle-orm/pg-core"
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
	market: text().default('US').notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("sectors_user_id_idx").using("btree", table.userId.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "sectors_user_id_fkey"
		}).onDelete("cascade"),
	unique("sectors_user_id_name_key").on(table.userId, table.name),
	check("sectors_market_check", sql`market = ANY (ARRAY['US'::text, 'CN'::text])`),
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

export const rawPayloads = pgTable("raw_payloads", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	source: text().notNull(),
	endpoint: text().notNull(),
	symbol: text().notNull(),
	asOf: date("as_of").notNull(),
	body: jsonb().notNull(),
	fetchedAt: timestamp("fetched_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("raw_payloads_symbol_idx").using("btree", table.symbol.asc().nullsLast().op("text_ops")),
	unique("raw_payloads_source_endpoint_symbol_as_of_key").on(table.source, table.endpoint, table.symbol, table.asOf),
	pgPolicy("raw_payloads_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
]);

export const briefs = pgTable("briefs", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	userId: uuid("user_id").notNull(),
	sessionDate: date("session_date").notNull(),
	kind: text().notNull(),
	schemaVersion: integer("schema_version").notNull(),
	body: jsonb().notNull(),
	createdAt: timestamp("created_at", { withTimezone: true, mode: 'string' }).defaultNow().notNull(),
}, (table) => [
	index("briefs_user_date_idx").using("btree", table.userId.asc().nullsLast().op("date_ops"), table.sessionDate.asc().nullsLast().op("uuid_ops")),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "briefs_user_id_fkey"
		}).onDelete("cascade"),
	unique("briefs_user_id_session_date_kind_key").on(table.userId, table.sessionDate, table.kind),
	pgPolicy("briefs_tenant", { as: "permissive", for: "all", to: ["public"], using: sql`(user_id = auth.uid())`, withCheck: sql`(user_id = auth.uid())`  }),
	check("briefs_kind_check", sql`kind = ANY (ARRAY['open'::text, 'close'::text])`),
]);

export const metrics = pgTable("metrics", {
	userId: uuid("user_id").notNull(),
	symbol: text().notNull(),
	sessionDate: date("session_date").notNull(),
	metric: text().notNull(),
	value: numeric().notNull(),
}, (table) => [
	index("metrics_user_date_idx").using("btree", table.userId.asc().nullsLast().op("date_ops"), table.sessionDate.asc().nullsLast().op("date_ops")),
	foreignKey({
			columns: [table.userId],
			foreignColumns: [users.id],
			name: "metrics_user_id_fkey"
		}).onDelete("cascade"),
	primaryKey({ columns: [table.userId, table.symbol, table.sessionDate, table.metric], name: "metrics_pkey"}),
	pgPolicy("metrics_tenant", { as: "permissive", for: "all", to: ["public"], using: sql`(user_id = auth.uid())`, withCheck: sql`(user_id = auth.uid())`  }),
]);

export const barsDaily = pgTable("bars_daily", {
	symbol: text().notNull(),
	sessionDate: date("session_date").notNull(),
	o: numeric().notNull(),
	h: numeric().notNull(),
	l: numeric().notNull(),
	c: numeric().notNull(),
	// You can use { mode: "bigint" } if numbers are exceeding js number limitations
	v: bigint({ mode: "number" }).notNull(),
	adjC: numeric("adj_c").notNull(),
}, (table) => [
	primaryKey({ columns: [table.symbol, table.sessionDate], name: "bars_daily_pkey"}),
	pgPolicy("bars_daily_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
]);

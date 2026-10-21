import { pgTable, varchar, unique, pgPolicy, uuid, text, timestamp, index, foreignKey, integer, check, numeric, bigint, date, jsonb, primaryKey, smallint, boolean } from "drizzle-orm/pg-core"
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
	check("briefs_kind_check", sql`kind = ANY (ARRAY['open'::text, 'close'::text, 'open_cn'::text, 'close_cn'::text])`),
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

export const events = pgTable("events", {
	id: uuid().defaultRandom().primaryKey().notNull(),
	symbol: text(),
	eventType: text("event_type").notNull(),
	occursAt: date("occurs_at").notNull(),
	payload: jsonb().default({}).notNull(),
	label: text(),
}, (table) => [
	index("events_symbol_occurs_idx").using("btree", table.symbol.asc().nullsLast().op("text_ops"), table.occursAt.asc().nullsLast().op("date_ops")),
	unique("events_calendar_key").on(table.symbol, table.eventType, table.occursAt).nullsNotDistinct(),
	pgPolicy("events_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
	check("events_event_type_check", sql`event_type = ANY (ARRAY['earnings'::text, 'lockup'::text, 'ex_div'::text, 'macro'::text])`),
]);

export const optionContracts = pgTable("option_contracts", {
	contract: text().primaryKey().notNull(),
	symbol: text().notNull(),
	expirationDate: date("expiration_date").notNull(),
	putOrCall: text("put_or_call").notNull(),
	strike: numeric().notNull(),
	firstSeen: date("first_seen").notNull(),
}, (table) => [
	index("option_contracts_symbol_expiry_idx").using("btree", table.symbol.asc().nullsLast().op("text_ops"), table.expirationDate.asc().nullsLast().op("date_ops")),
	pgPolicy("option_contracts_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
	check("option_contracts_put_or_call_check", sql`put_or_call = ANY (ARRAY['C'::text, 'P'::text])`),
]);

export const optionSnapshot = pgTable("option_snapshot", {
	contract: text().notNull(),
	sessionDate: date("session_date").notNull(),
	bid: numeric().notNull(),
	bidSize: integer("bid_size"),
	ask: numeric().notNull(),
	askSize: integer("ask_size"),
	last: numeric(),
	iv: numeric(),
	delta: numeric(),
	gamma: numeric(),
	theta: numeric(),
	vega: numeric(),
	rho: numeric(),
	openInterest: integer("open_interest").notNull(),
	volume: integer().notNull(),
	prevDayClose: numeric("prev_day_close"),
	capturedAt: timestamp("captured_at", { withTimezone: true, mode: 'string' }).notNull(),
}, (table) => [
	foreignKey({
			columns: [table.contract],
			foreignColumns: [optionContracts.contract],
			name: "option_snapshot_contract_fkey"
		}),
	primaryKey({ columns: [table.contract, table.sessionDate], name: "option_snapshot_pkey"}),
	pgPolicy("option_snapshot_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
]);

export const optionStrip = pgTable("option_strip", {
	symbol: text().notNull(),
	sessionDate: date("session_date").notNull(),
	tenor: smallint().notNull(),
	strikeRule: text("strike_rule").notNull(),
	contract: text(),
	dte: smallint(),
	moneyness: numeric(),
	mid: numeric(),
	premiumPct: numeric("premium_pct"),
	timeValuePct: numeric("time_value_pct"),
	iv: numeric(),
	delta: numeric(),
	openInterest: integer("open_interest"),
	volume: integer(),
	chgContractPct: numeric("chg_contract_pct"),
	chgTenorPct: numeric("chg_tenor_pct"),
	ivChg: numeric("iv_chg"),
	tenorCollapsed: boolean("tenor_collapsed").default(false).notNull(),
	targetMiss: boolean("target_miss").default(false).notNull(),
}, (table) => [
	primaryKey({ columns: [table.symbol, table.sessionDate, table.tenor, table.strikeRule], name: "option_strip_pkey"}),
	pgPolicy("option_strip_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
	check("option_strip_strike_rule_check", sql`strike_rule = ANY (ARRAY['itm10'::text, 'atm'::text, 'otm10'::text])`),
]);

export const indexDaily = pgTable("index_daily", {
	symbol: text().notNull(),
	sessionDate: date("session_date").notNull(),
	o: numeric(),
	h: numeric(),
	l: numeric(),
	c: numeric().notNull(),
}, (table) => [
	primaryKey({ columns: [table.symbol, table.sessionDate], name: "index_daily_pkey"}),
	pgPolicy("index_daily_read", { as: "permissive", for: "select", to: ["public"], using: sql`true` }),
]);

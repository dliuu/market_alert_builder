import { relations } from "drizzle-orm/relations";
import { users, sectors, holdings, lots } from "./schema";

export const sectorsRelations = relations(sectors, ({one, many}) => ({
	user: one(users, {
		fields: [sectors.userId],
		references: [users.id]
	}),
	holdings: many(holdings),
}));

export const usersRelations = relations(users, ({many}) => ({
	sectors: many(sectors),
	holdings: many(holdings),
	lots: many(lots),
}));

export const holdingsRelations = relations(holdings, ({one, many}) => ({
	sector: one(sectors, {
		fields: [holdings.sectorId],
		references: [sectors.id]
	}),
	user: one(users, {
		fields: [holdings.userId],
		references: [users.id]
	}),
	lots: many(lots),
}));

export const lotsRelations = relations(lots, ({one}) => ({
	holding: one(holdings, {
		fields: [lots.holdingId],
		references: [holdings.id]
	}),
	user: one(users, {
		fields: [lots.userId],
		references: [users.id]
	}),
}));
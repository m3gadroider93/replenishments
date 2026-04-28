# Copyright (c) 2026, ME and contributors
# For license information, please see license.txt

import math

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, flt, getdate, nowdate


class ReplenishmentRun(Document):
	def validate(self):
		if not self.run_date:
			self.run_date = nowdate()
		if not self.status:
			self.status = "Draft"
		if flt(self.lookback_days) <= 0:
			frappe.throw(_("Lookback Days must be greater than zero"))
		if flt(self.safety_stock_days) < 0:
			frappe.throw(_("Safety Stock Days cannot be negative"))

	def on_submit(self):
		self.db_set("status", "Submitted")

	def on_cancel(self):
		self.db_set("status", "Draft")

	@frappe.whitelist()
	def calculate(self):
		if self.docstatus != 0:
			frappe.throw(_("Cannot recalculate a submitted Replenishment Run"))

		warehouse = self.warehouse
		lookback_days = int(self.lookback_days or 90)
		safety_stock_days = int(self.safety_stock_days or 14)
		run_date = getdate(self.run_date or nowdate())
		from_date = add_days(run_date, -lookback_days)

		stock_map = _get_current_stock(warehouse)
		usage_map = _get_issued_qty(warehouse, from_date, run_date)
		open_po_map = _get_open_po_qty(warehouse)

		candidate_items = set(stock_map) | set(usage_map)
		self.set("suggestions", [])
		self.set("skipped_items", [])

		if not candidate_items:
			self.status = "Calculated"
			self.save()
			return {"rows": 0, "skipped": 0}

		item_map = _get_item_details(candidate_items)
		supplier_map = _get_default_suppliers(candidate_items)
		min_stock_map = _get_min_stock(candidate_items, warehouse)

		for item_code in sorted(candidate_items):
			current_stock = flt(stock_map.get(item_code, 0.0))
			open_po_qty = flt(open_po_map.get(item_code, 0.0))
			supplier = supplier_map.get(item_code)

			item = item_map.get(item_code)
			if not item:
				self.append(
					"skipped_items",
					{
						"item": item_code,
						"default_supplier": supplier,
						"reason": "Item Disabled Or Non-Stock",
						"current_stock": current_stock,
						"open_po_qty": open_po_qty,
						"avg_daily_usage": 0,
						"reorder_point": 0,
						"suggested_qty": 0,
					},
				)
				continue

			avg_daily_usage = flt(usage_map.get(item_code, 0.0)) / lookback_days
			lead_time_days = int(item.get("lead_time_days") or 7)
			safety_stock_qty = avg_daily_usage * safety_stock_days
			reorder_point = (avg_daily_usage * lead_time_days) + safety_stock_qty

			min_stock_info = min_stock_map.get(item_code) or {}
			min_stock = flt(min_stock_info.get("min_stock"))
			min_stock_reorder_qty = flt(min_stock_info.get("reorder_qty"))

			breach_reorder = avg_daily_usage > 0 and (current_stock + open_po_qty < reorder_point)
			breach_min_stock = min_stock > 0 and current_stock < min_stock

			if not breach_reorder and not breach_min_stock:
				if avg_daily_usage <= 0 and min_stock <= 0:
					reason = "No Usage In Lookback"
				else:
					reason = "Sufficient Stock"
				self.append(
					"skipped_items",
					{
						"item": item_code,
						"default_supplier": supplier,
						"reason": reason,
						"current_stock": current_stock,
						"open_po_qty": open_po_qty,
						"avg_daily_usage": avg_daily_usage,
						"reorder_point": reorder_point,
						"suggested_qty": 0,
					},
				)
				continue

			if avg_daily_usage > 0:
				target_qty = reorder_point + (avg_daily_usage * lead_time_days)
			else:
				target_qty = 0.0

			if min_stock > 0:
				min_stock_target = min_stock + min_stock_reorder_qty
				target_qty = max(target_qty, min_stock_target)

			suggested_qty = target_qty - current_stock - open_po_qty
			if suggested_qty <= 0:
				self.append(
					"skipped_items",
					{
						"item": item_code,
						"default_supplier": supplier,
						"reason": "Sufficient Stock",
						"current_stock": current_stock,
						"open_po_qty": open_po_qty,
						"avg_daily_usage": avg_daily_usage,
						"reorder_point": reorder_point,
						"suggested_qty": 0,
					},
				)
				continue

			min_order_qty = flt(item.get("min_order_qty"))
			if min_order_qty > 0:
				multiples = math.ceil(suggested_qty / min_order_qty)
				suggested_qty = multiples * min_order_qty
			else:
				suggested_qty = math.ceil(suggested_qty)

			self.append(
				"suggestions",
				{
					"item": item_code,
					"default_supplier": supplier,
					"current_stock": current_stock,
					"avg_daily_usage": avg_daily_usage,
					"lead_time_days": lead_time_days,
					"reorder_point": reorder_point,
					"open_po_qty": open_po_qty,
					"suggested_qty": suggested_qty,
				},
			)

		self.status = "Calculated"
		self.save()
		return {"rows": len(self.suggestions), "skipped": len(self.skipped_items)}


def _get_current_stock(warehouse):
	rows = frappe.get_all(
		"Bin",
		filters={"warehouse": warehouse},
		fields=["item_code", "actual_qty"],
	)
	return {r.item_code: flt(r.actual_qty) for r in rows}


def _get_issued_qty(warehouse, from_date, to_date):
	sle = frappe.qb.DocType("Stock Ledger Entry")
	rows = (
		frappe.qb.from_(sle)
		.select(sle.item_code, Sum(sle.actual_qty).as_("qty"))
		.where(
			(sle.warehouse == warehouse)
			& (sle.docstatus == 1)
			& (sle.is_cancelled == 0)
			& (sle.posting_date >= from_date)
			& (sle.posting_date <= to_date)
			& (sle.actual_qty < 0)
		)
		.groupby(sle.item_code)
	).run(as_dict=True)
	return {r["item_code"]: -flt(r["qty"]) for r in rows}


def _get_open_po_qty(warehouse):
	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	rows = (
		frappe.qb.from_(poi)
		.inner_join(po)
		.on(po.name == poi.parent)
		.select(
			poi.item_code,
			Sum(poi.qty - poi.received_qty).as_("qty"),
		)
		.where(
			(poi.warehouse == warehouse)
			& (po.docstatus == 1)
			& (po.status.notin(["Closed", "Completed", "Delivered", "Cancelled"]))
			& (poi.qty > poi.received_qty)
		)
		.groupby(poi.item_code)
	).run(as_dict=True)
	return {r["item_code"]: flt(r["qty"]) for r in rows}


def _get_item_details(item_codes):
	if not item_codes:
		return {}
	rows = frappe.get_all(
		"Item",
		filters={
			"name": ("in", list(item_codes)),
			"is_stock_item": 1,
			"disabled": 0,
		},
		fields=["name", "lead_time_days", "min_order_qty", "safety_stock"],
	)
	return {r.name: r for r in rows}


def _get_min_stock(item_codes, warehouse):
	if not item_codes:
		return {}

	out = {}

	reorder_rows = frappe.get_all(
		"Item Reorder",
		filters={"parent": ("in", list(item_codes)), "warehouse": warehouse},
		fields=["parent", "warehouse_reorder_level", "warehouse_reorder_qty"],
	)
	for r in reorder_rows:
		level = flt(r.warehouse_reorder_level)
		if level > 0:
			out[r.parent] = {
				"min_stock": level,
				"reorder_qty": flt(r.warehouse_reorder_qty),
			}

	missing = [code for code in item_codes if code not in out]
	if missing:
		safety_rows = frappe.get_all(
			"Item",
			filters={"name": ("in", missing)},
			fields=["name", "safety_stock"],
		)
		for r in safety_rows:
			level = flt(r.safety_stock)
			if level > 0:
				out[r.name] = {"min_stock": level, "reorder_qty": 0.0}

	return out


def _get_default_suppliers(item_codes):
	if not item_codes:
		return {}
	rows = frappe.get_all(
		"Item Default",
		filters={"parent": ("in", list(item_codes))},
		fields=["parent", "default_supplier"],
		order_by="idx asc",
	)
	out = {}
	for r in rows:
		if r.default_supplier:
			out.setdefault(r.parent, r.default_supplier)
	return out

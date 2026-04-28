// Copyright (c) 2026, ME and contributors
// For license information, please see license.txt

frappe.ui.form.on("Replenishment Run", {
	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Calculate"), () => {
				if (!frm.doc.warehouse) {
					frappe.msgprint(__("Please set a Warehouse first"));
					return;
				}

				const run = () =>
					frm
						.call({
							method: "calculate",
							doc: frm.doc,
							freeze: true,
							freeze_message: __("Calculating replenishment suggestions..."),
						})
						.then((r) => {
							frm.reload_doc();
							const rows = (r && r.message && r.message.rows) || 0;
							const skipped = (r && r.message && r.message.skipped) || 0;
							frappe.show_alert({
								message: __("Calculated {0} suggestion(s), {1} skipped", [rows, skipped]),
								indicator: rows ? "green" : "orange",
							});
						});

				if (frm.is_dirty()) {
					frm.save().then(run);
				} else {
					run();
				}
			}).addClass("btn-primary");
		}
	},
});

frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		if (!frm.doc.epost_letter || frm.is_new()) {
			return;
		}
		frm.add_custom_button(
			__("Open ePost Letter"),
			() => frappe.set_route("Form", "ePost Letter", frm.doc.epost_letter),
			__("ePost")
		);
		// Changing the supplier here would keep the accounts suggested for the
		// old one and teach the matcher nothing; the review rebuilds the draft.
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(
				__("Wrong Supplier or Type"),
				() => {
					frappe.route_options = { epost_review: 1 };
					frappe.set_route("Form", "ePost Letter", frm.doc.epost_letter);
				},
				__("ePost")
			);
		}
	},
});

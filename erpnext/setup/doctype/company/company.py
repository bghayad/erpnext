# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, os, json
from frappe import _
from frappe.utils import get_timestamp

from frappe.utils import cint, today, formatdate
import frappe.defaults
from frappe.cache_manager import clear_defaults_cache

from frappe.model.document import Document
from frappe.contacts.address_and_contact import load_address_and_contact
from frappe.utils.nestedset import NestedSet
from erpnext.accounts.party import validate_party_accounts, get_dashboard_info, get_timeline_data # keep this


class Company(NestedSet):
	nsm_parent_field = 'parent_company'

	def onload(self):
		load_address_and_contact(self, "company")
		self.get("__onload")["transactions_exist"] = self.check_if_transactions_exist()
		self.load_dashboard_info()

	def load_dashboard_info(self):
		info = get_dashboard_info(self.doctype, self.name)
		self.set_onload('dashboard_info', info)

	def check_if_transactions_exist(self):
		exists = False
		for doctype in ["Sales Invoice", "Delivery Note", "Sales Order", "Quotation",
			"Purchase Invoice", "Purchase Receipt", "Purchase Order", "Supplier Quotation"]:
				if frappe.db.sql("""select name from `tab%s` where company=%s and docstatus=1
					limit 1""" % (doctype, "%s"), self.name):
						exists = True
						break

		return exists

	def validate(self):
		self.validate_abbr()
		self.validate_default_accounts()
		self.validate_currency()
		self.validate_coa_input()
		self.validate_perpetual_inventory()
		self.check_country_change()
		self.set_chart_of_accounts()

	def validate_abbr(self):
		if not self.abbr:
			self.abbr = ''.join([c[0] for c in self.company_name.split()]).upper()

		self.abbr = self.abbr.strip()

		# if self.get('__islocal') and len(self.abbr) > 5:
		# 	frappe.throw(_("Abbreviation cannot have more than 5 characters"))

		if not self.abbr.strip():
			frappe.throw(_("Abbreviation is mandatory"))

		if frappe.db.sql("select abbr from tabCompany where name!=%s and abbr=%s", (self.name, self.abbr)):
			frappe.throw(_("Abbreviation already used for another company"))

	def create_default_tax_template(self):
		from erpnext.setup.setup_wizard.operations.taxes_setup import create_sales_tax
		create_sales_tax({
			'country': self.country,
			'company_name': self.name
		})

	def validate_default_accounts(self):
		for field in ["default_bank_account", "default_cash_account",
			"default_receivable_account", "default_payable_account",
			"default_expense_account", "default_income_account",
			"stock_received_but_not_billed", "stock_adjustment_account",
			"expenses_included_in_valuation", "default_payroll_payable_account"]:
				if self.get(field):
					for_company = frappe.db.get_value("Account", self.get(field), "company")
					if for_company != self.name:
						frappe.throw(_("Account {0} does not belong to company: {1}")
							.format(self.get(field), self.name))

	def validate_currency(self):
		if self.is_new():
			return
		self.previous_default_currency = frappe.get_cached_value('Company',  self.name,  "default_currency")
		if self.default_currency and self.previous_default_currency and \
			self.default_currency != self.previous_default_currency and \
			self.check_if_transactions_exist():
				frappe.throw(_("Cannot change company's default currency, because there are existing transactions. Transactions must be cancelled to change the default currency."))

	def on_update(self):
		NestedSet.on_update(self)
		if not frappe.db.sql("""select name from tabAccount
				where company=%s and docstatus<2 limit 1""", self.name):
			if not frappe.local.flags.ignore_chart_of_accounts:
				frappe.flags.country_change = True
				self.create_default_accounts()
				self.create_default_warehouses()

		if frappe.flags.country_change:
			install_country_fixtures(self.name)
			self.create_default_tax_template()



		if not frappe.db.get_value("Department", {"company": self.name}):
			from erpnext.setup.setup_wizard.operations.install_fixtures import install_post_company_fixtures
			install_post_company_fixtures(frappe._dict({'company_name': self.name}))

		if not frappe.db.get_value("Cost Center", {"is_group": 0, "company": self.name}):
			self.create_default_cost_center()

		if not frappe.local.flags.ignore_chart_of_accounts:
			self.set_default_accounts()
			if self.default_cash_account:
				self.set_mode_of_payment_account()

		if self.default_currency:
			frappe.db.set_value("Currency", self.default_currency, "enabled", 1)

		if hasattr(frappe.local, 'enable_perpetual_inventory') and \
			self.name in frappe.local.enable_perpetual_inventory:
			frappe.local.enable_perpetual_inventory[self.name] = self.enable_perpetual_inventory

		frappe.clear_cache()

	def create_default_warehouses(self):
		for wh_detail in [
			{"warehouse_name": _("All Warehouses"), "is_group": 1},
			{"warehouse_name": _("Stores"), "is_group": 0},
			{"warehouse_name": _("Work In Progress"), "is_group": 0},
			{"warehouse_name": _("Finished Goods"), "is_group": 0}]:

			if not frappe.db.exists("Warehouse", "{0} - {1}".format(wh_detail["warehouse_name"], self.abbr)):
				warehouse = frappe.get_doc({
					"doctype":"Warehouse",
					"warehouse_name": wh_detail["warehouse_name"],
					"is_group": wh_detail["is_group"],
					"company": self.name,
					"parent_warehouse": "{0} - {1}".format(_("All Warehouses"), self.abbr) \
						if not wh_detail["is_group"] else ""
				})
				warehouse.flags.ignore_permissions = True
				warehouse.flags.ignore_mandatory = True
				warehouse.insert()

	def create_default_accounts(self):
		from erpnext.accounts.doctype.account.chart_of_accounts.chart_of_accounts import create_charts
		frappe.local.flags.ignore_root_company_validation = True
		create_charts(self.name, self.chart_of_accounts, self.existing_company)

		frappe.db.set(self, "default_receivable_account", frappe.db.get_value("Account",
			{"company": self.name, "account_type": "Receivable", "is_group": 0}))
		frappe.db.set(self, "default_payable_account", frappe.db.get_value("Account",
			{"company": self.name, "account_type": "Payable", "is_group": 0}))

	def validate_coa_input(self):
		if self.create_chart_of_accounts_based_on == "Existing Company":
			self.chart_of_accounts = None
			if not self.existing_company:
				frappe.throw(_("Please select Existing Company for creating Chart of Accounts"))

		else:
			self.existing_company = None
			self.create_chart_of_accounts_based_on = "Standard Template"
			if not self.chart_of_accounts:
				self.chart_of_accounts = "Standard"

	def validate_perpetual_inventory(self):
		if not self.get("__islocal"):
			if cint(self.enable_perpetual_inventory) == 1 and not self.default_inventory_account:
				frappe.msgprint(_("Set default inventory account for perpetual inventory"),
					alert=True, indicator='orange')

	def check_country_change(self):
		frappe.flags.country_change = False

		if not self.get('__islocal') and \
			self.country != frappe.get_cached_value('Company',  self.name,  'country'):
			frappe.flags.country_change = True

	def set_chart_of_accounts(self):
		''' If parent company is set, chart of accounts will be based on that company '''
		if self.parent_company:
			self.create_chart_of_accounts_based_on = "Existing Company"
			self.existing_company = self.parent_company

	def set_default_accounts(self):
		self._set_default_account("default_cash_account", "Cash")
		self._set_default_account("default_bank_account", "Bank")
		self._set_default_account("round_off_account", "Round Off")
		self._set_default_account("accumulated_depreciation_account", "Accumulated Depreciation")
		self._set_default_account("depreciation_expense_account", "Depreciation")
		self._set_default_account("capital_work_in_progress_account", "Capital Work in Progress")
		self._set_default_account("asset_received_but_not_billed", "Asset Received But Not Billed")
		self._set_default_account("expenses_included_in_asset_valuation", "Expenses Included In Asset Valuation")

		if self.enable_perpetual_inventory:
			self._set_default_account("stock_received_but_not_billed", "Stock Received But Not Billed")
			self._set_default_account("default_inventory_account", "Stock")
			self._set_default_account("stock_adjustment_account", "Stock Adjustment")
			self._set_default_account("expenses_included_in_valuation", "Expenses Included In Valuation")
			self._set_default_account("default_expense_account", "Cost of Goods Sold")

		if not self.default_income_account:
			income_account = frappe.db.get_value("Account",
				{"account_name": _("Sales"), "company": self.name, "is_group": 0})

			if not income_account:
				income_account = frappe.db.get_value("Account",
					{"account_name": _("Sales Account"), "company": self.name})

			self.db_set("default_income_account", income_account)

		if not self.default_payable_account:
			self.db_set("default_payable_account", self.default_payable_account)

		if not self.default_payroll_payable_account:
			payroll_payable_account = frappe.db.get_value("Account",
				{"account_name": _("Payroll Payable"), "company": self.name, "is_group": 0})

			self.db_set("default_payroll_payable_account", payroll_payable_account)

		if not self.default_employee_advance_account:
			employe_advance_account = frappe.db.get_value("Account",
				{"account_name": _("Employee Advances"), "company": self.name, "is_group": 0})

			self.db_set("default_employee_advance_account", employe_advance_account)

		if not self.write_off_account:
			write_off_acct = frappe.db.get_value("Account",
				{"account_name": _("Write Off"), "company": self.name, "is_group": 0})

			self.db_set("write_off_account", write_off_acct)

		if not self.exchange_gain_loss_account:
			exchange_gain_loss_acct = frappe.db.get_value("Account",
				{"account_name": _("Exchange Gain/Loss"), "company": self.name, "is_group": 0})

			self.db_set("exchange_gain_loss_account", exchange_gain_loss_acct)

		if not self.disposal_account:
			disposal_acct = frappe.db.get_value("Account",
				{"account_name": _("Gain/Loss on Asset Disposal"), "company": self.name, "is_group": 0})

			self.db_set("disposal_account", disposal_acct)

	def _set_default_account(self, fieldname, account_type):
		if self.get(fieldname):
			return

		account = frappe.db.get_value("Account", {"account_type": account_type,
			"is_group": 0, "company": self.name})

		if account:
			self.db_set(fieldname, account)

	def set_mode_of_payment_account(self):
		cash = frappe.db.get_value('Mode of Payment', {'type': 'Cash'}, 'name')
		if cash and self.default_cash_account \
				and not frappe.db.get_value('Mode of Payment Account', {'company': self.name, 'parent': cash}):
			mode_of_payment = frappe.get_doc('Mode of Payment', cash)
			mode_of_payment.append('accounts', {
				'company': self.name,
				'default_account': self.default_cash_account
			})
			mode_of_payment.save(ignore_permissions=True)

	def create_default_cost_center(self):
		cc_list = [
			{
				'cost_center_name': self.name,
				'company':self.name,
				'is_group': 1,
				'parent_cost_center':None
			},
			{
				'cost_center_name':_('Main'),
				'company':self.name,
				'is_group':0,
				'parent_cost_center':self.name + ' - ' + self.abbr
			},
		]
		for cc in cc_list:
			cc.update({"doctype": "Cost Center"})
			cc_doc = frappe.get_doc(cc)
			cc_doc.flags.ignore_permissions = True

			if cc.get("cost_center_name") == self.name:
				cc_doc.flags.ignore_mandatory = True
			cc_doc.insert()

		frappe.db.set(self, "cost_center", _("Main") + " - " + self.abbr)
		frappe.db.set(self, "round_off_cost_center", _("Main") + " - " + self.abbr)
		frappe.db.set(self, "depreciation_cost_center", _("Main") + " - " + self.abbr)

	def after_rename(self, olddn, newdn, merge=False):
		frappe.db.set(self, "company_name", newdn)

		frappe.db.sql("""update `tabDefaultValue` set defvalue=%s
			where defkey='Company' and defvalue=%s""", (newdn, olddn))

		clear_defaults_cache()

	def abbreviate(self):
		self.abbr = ''.join([c[0].upper() for c in self.company_name.split()])

	def on_trash(self):
		"""
			Trash accounts and cost centers for this company if no gl entry exists
		"""
		NestedSet.validate_if_child_exists(self)
		frappe.utils.nestedset.update_nsm(self)

		rec = frappe.db.sql("SELECT name from `tabGL Entry` where company = %s", self.name)
		if not rec:
			frappe.db.sql("""delete from `tabBudget Account`
				where exists(select name from tabBudget
					where name=`tabBudget Account`.parent and company = %s)""", self.name)

			for doctype in ["Account", "Cost Center", "Budget", "Party Account"]:
				frappe.db.sql("delete from `tab{0}` where company = %s".format(doctype), self.name)

		if not frappe.db.get_value("Stock Ledger Entry", {"company": self.name}):
			frappe.db.sql("""delete from `tabWarehouse` where company=%s""", self.name)

		frappe.defaults.clear_default("company", value=self.name)
		for doctype in ["Mode of Payment Account", "Item Default"]:
			frappe.db.sql("delete from `tab{0}` where company = %s".format(doctype), self.name)

		# clear default accounts, warehouses from item
		warehouses = frappe.db.sql_list("select name from tabWarehouse where company=%s", self.name)
		if warehouses:
			frappe.db.sql("""delete from `tabItem Reorder` where warehouse in (%s)"""
				% ', '.join(['%s']*len(warehouses)), tuple(warehouses))

		# reset default company
		frappe.db.sql("""update `tabSingles` set value=""
			where doctype='Global Defaults' and field='default_company'
			and value=%s""", self.name)

		# delete BOMs
		boms = frappe.db.sql_list("select name from tabBOM where company=%s", self.name)
		if boms:
			frappe.db.sql("delete from tabBOM where company=%s", self.name)
			for dt in ("BOM Operation", "BOM Item", "BOM Scrap Item", "BOM Explosion Item"):
				frappe.db.sql("delete from `tab%s` where parent in (%s)"""
					% (dt, ', '.join(['%s']*len(boms))), tuple(boms))

		frappe.db.sql("delete from tabEmployee where company=%s", self.name)
		frappe.db.sql("delete from tabDepartment where company=%s", self.name)
		frappe.db.sql("delete from `tabTax Withholding Account` where company=%s", self.name)

		frappe.db.sql("delete from `tabSales Taxes and Charges Template` where company=%s", self.name)
		frappe.db.sql("delete from `tabPurchase Taxes and Charges Template` where company=%s", self.name)

@frappe.whitelist()
def enqueue_replace_abbr(company, old, new):
	kwargs = dict(company=company, old=old, new=new)
	frappe.enqueue('erpnext.setup.doctype.company.company.replace_abbr', **kwargs)


@frappe.whitelist()
def replace_abbr(company, old, new):
	new = new.strip()
	if not new:
		frappe.throw(_("Abbr can not be blank or space"))

	frappe.only_for("System Manager")

	frappe.db.set_value("Company", company, "abbr", new)

	def _rename_record(doc):
		parts = doc[0].rsplit(" - ", 1)
		if len(parts) == 1 or parts[1].lower() == old.lower():
			frappe.rename_doc(dt, doc[0], parts[0] + " - " + new)

	def _rename_records(dt):
		# rename is expensive so let's be economical with memory usage
		doc = (d for d in frappe.db.sql("select name from `tab%s` where company=%s" % (dt, '%s'), company))
		for d in doc:
			_rename_record(d)

	for dt in ["Warehouse", "Account", "Cost Center", "Department",
			"Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"]:
		_rename_records(dt)
		frappe.db.commit()


def get_name_with_abbr(name, company):
	company_abbr = frappe.get_cached_value('Company',  company,  "abbr")
	parts = name.split(" - ")

	if parts[-1].lower() != company_abbr.lower():
		parts.append(company_abbr)

	return " - ".join(parts)

def install_country_fixtures(company):
	company_doc = frappe.get_doc("Company", company)
	path = frappe.get_app_path('erpnext', 'regional', frappe.scrub(company_doc.country))
	if os.path.exists(path.encode("utf-8")):
		frappe.get_attr("erpnext.regional.{0}.setup.setup"
			.format(frappe.scrub(company_doc.country)))(company_doc, False)

def update_company_current_month_sales(company):
	current_month_year = formatdate(today(), "MM-yyyy")

	results = frappe.db.sql('''
		select
			sum(base_grand_total) as total, date_format(posting_date, '%m-%Y') as month_year
		from
			`tabSales Invoice`
		where
			date_format(posting_date, '%m-%Y')="{0}"
			and docstatus = 1
			and company = "{1}"
		group by
			month_year
	'''.format(current_month_year, frappe.db.escape(company)), as_dict = True)

	monthly_total = results[0]['total'] if len(results) > 0 else 0

	results = frappe.db.sql('''
		select
			sum(cust_grand_total) as total, date_format(posting_date, '%m-%Y') as month_year
		from
			`tabTicket Invoice`
		where
			date_format(posting_date, '%m-%Y')="{0}"
			and docstatus = 1
			and company = "{1}"
		group by
			month_year
	'''.format(current_month_year, frappe.db.escape(company)), as_dict = True)

	monthly_total = monthly_total + (results[0]['total'] if len(results) > 0 else 0)

	results = frappe.db.sql('''
		select
			sum(base_cust_grand_total) as total, date_format(posting_date, '%m-%Y') as month_year
		from
			`tabTour Invoice`
		where
			date_format(posting_date, '%m-%Y')="{0}"
			and docstatus = 1
			and company = "{1}"
		group by
			month_year
	'''.format(current_month_year, frappe.db.escape(company)), as_dict = True)

	monthly_total = monthly_total + (results[0]['total'] if len(results) > 0 else 0)

	frappe.db.set_value("Company", company, "total_monthly_sales", monthly_total)


def update_company_current_month_purchase(company):
	current_month_year = formatdate(today(), "MM-yyyy")

	results = frappe.db.sql('''
		select
			sum(base_grand_total) as total, date_format(posting_date, '%m-%Y') as month_year
		from
			`tabPurchase Invoice`
		where
			date_format(posting_date, '%m-%Y')="{0}"
			and docstatus = 1
			and company = "{1}"
		group by
			month_year
	'''.format(current_month_year, frappe.db.escape(company)), as_dict = True)

	monthly_total = results[0]['total'] if len(results) > 0 else 0

	results = frappe.db.sql('''
		select
			sum(supp_grand_total) as total, date_format(posting_date, '%m-%Y') as month_year
		from
			`tabTicket Invoice`
		where
			date_format(posting_date, '%m-%Y')="{0}"
			and docstatus = 1
			and company = "{1}"
		group by
			month_year
	'''.format(current_month_year, frappe.db.escape(company)), as_dict = True)

	monthly_total = monthly_total + (results[0]['total'] if len(results) > 0 else 0)

	results = frappe.db.sql('''
		select
			sum(base_supp_grand_total) as total, date_format(posting_date, '%m-%Y') as month_year
		from
			`tabTour Invoice`
		where
			date_format(posting_date, '%m-%Y')="{0}"
			and docstatus = 1
			and company = "{1}"
		group by
			month_year
	'''.format(current_month_year, frappe.db.escape(company)), as_dict = True)

	monthly_total = monthly_total + (results[0]['total'] if len(results) > 0 else 0)

	frappe.db.set_value("Company", company, "total_monthly_purchase", monthly_total)

def update_company_monthly_sales(company):
	'''Cache past year monthly sales of every company based on sales invoices'''
#	from frappe.utils.goal import get_monthly_results
	import json
	filter_str = "company = '{0}' and status != 'Draft' and docstatus=1".format(frappe.db.escape(company))
#	month_to_value_dict = get_monthly_results("Sales Invoice", "base_grand_total",
#		"posting_date", filter_str, "sum")

	doctypes = "[Sales Invoice, Purchase Invoice, Ticket Invoice, Tour Invoice]" 
	month_to_value_dict = get_monthly_results(['Sales Invoice', 'Purchase Invoice', 'Ticket Invoice', 'Tour Invoice'], "base_grand_total",
		"posting_date", filter_str, "sum")

	frappe.msgprint(_("From update_company_monthly_sales the Sales month_to_value_dict is {0} and the Purchase month_to_value_dict is {1}").format(month_to_value_dict[0], month_to_value_dict[1]), alert=True, indicator='red')

	frappe.db.set_value("Company", company, "sales_monthly_history", json.dumps(month_to_value_dict[0]))
	frappe.db.set_value("Company", company, "purchase_monthly_history", json.dumps(month_to_value_dict[1]))

def update_transactions_annual_history(company, commit=False):
	transactions_history = get_all_transactions_annual_history(company)
	frappe.db.set_value("Company", company, "transactions_annual_history", json.dumps(transactions_history))

	if commit:
		frappe.db.commit()

def cache_companies_monthly_sales_history():
	companies = [d['name'] for d in frappe.get_list("Company")]
	for company in companies:
		update_company_monthly_sales(company)
		update_transactions_annual_history(company)
	frappe.db.commit()

@frappe.whitelist()
def get_children(doctype, parent=None, company=None, is_root=False):
	if parent == None or parent == "All Companies":
		parent = ""

	return frappe.db.sql("""
		select
			name as value,
			is_group as expandable
		from
			`tab{doctype}` comp
		where
			ifnull(parent_company, "")="{parent}"
		""".format(
			doctype = frappe.db.escape(doctype),
			parent=frappe.db.escape(parent)
		), as_dict=1)

@frappe.whitelist()
def add_node():
	from frappe.desk.treeview import make_tree_args
	args = frappe.form_dict
	args = make_tree_args(**args)

	if args.parent_company == 'All Companies':
		args.parent_company = None

	frappe.get_doc(args).insert()

def get_all_transactions_annual_history(company):
	out = {}

	items = frappe.db.sql('''
		select transaction_date, count(*) as count

		from (
			select name, transaction_date, company
			from `tabQuotation`

			UNION ALL

			select name, transaction_date, company
			from `tabSales Order`

			UNION ALL

			select name, posting_date as transaction_date, company
			from `tabDelivery Note`

			UNION ALL

			select name, posting_date as transaction_date, company
			from `tabSales Invoice`

			UNION ALL

			select name, posting_date as transaction_date, company
			from `tabTicket Invoice`

			UNION ALL

			select name, posting_date as transaction_date, company
			from `tabTour Invoice`

			UNION ALL

			select name, creation as transaction_date, company
			from `tabIssue`

			UNION ALL

			select name, creation as transaction_date, company
			from `tabProject`
		) t

		where
			company=%s
			and
			transaction_date > date_sub(curdate(), interval 1 year)

		group by
			transaction_date
			''', (company), as_dict=True)

	for d in items:
		timestamp = get_timestamp(d["transaction_date"])
		out.update({ timestamp: d["count"] })

	return out

def get_timeline_data(doctype, name):
	'''returns timeline data based on linked records in dashboard'''
	out = {}
	date_to_value_dict = {}

	history = frappe.get_cached_value('Company',  name,  "transactions_annual_history")

	try:
		date_to_value_dict = json.loads(history) if history and '{' in history else None
	except ValueError:
		date_to_value_dict = None

	if date_to_value_dict is None:
		update_transactions_annual_history(name, True)
		history = frappe.get_cached_value('Company',  name,  "transactions_annual_history")
		return json.loads(history) if history and '{' in history else {}

	return date_to_value_dict

def get_monthly_results(goal_doctype, goal_field, date_col, filter_str, aggregation = 'sum'):
	'''Get monthly aggregation values for given field of doctype'''

#	frappe.msgprint(_("len of goal_doctype is {0} and first character is {1} and last character is {2}").format(len(goal_doctype), goal_doctype[0], 
#		goal_doctype[len(goal_doctype)-1]), alert=True, indicator='red')

	if (goal_doctype[0] == '[' and  goal_doctype[len(goal_doctype)-1] == ']'):
		goal_doctype_all = json.loads(goal_doctype)
	else:
		goal_doctype_all = goal_doctype
	goal_doctype1 = goal_doctype_all[0]

	where_clause = ('where ' + filter_str) if filter_str else ''
	sales_results = frappe.db.sql('''
		select
			{0}({1}) as {1}, date_format({2}, '%m-%Y') as month_year
		from
			`{3}`
		{4}
		group by
			month_year'''.format(aggregation, goal_field, date_col, "tab" +
			goal_doctype1, where_clause), as_dict=True)

	purchase_results = frappe.db.sql('''
		select
			{0}({1}) as {1}, date_format({2}, '%m-%Y') as month_year
		from
			`{3}`
		{4}
		group by
			month_year'''.format(aggregation, goal_field, date_col, "tab" +
			goal_doctype_all[1], where_clause), as_dict=True)

	ticket_goal_field = "cust_grand_total"

	sales_ticket_results = frappe.db.sql('''
		select
			{0}({1}) as {1}, date_format({2}, '%m-%Y') as month_year
		from
			`{3}`
		{4}
		group by
			month_year'''.format(aggregation, ticket_goal_field, date_col, "tab" +
			goal_doctype_all[2], where_clause), as_dict=True)

	purchase_ticket_goal_field = "supp_grand_total"

	purchase_ticket_results = frappe.db.sql('''
		select
			{0}({1}) as {1}, date_format({2}, '%m-%Y') as month_year
		from
			`{3}`
		{4}
		group by
			month_year'''.format(aggregation, purchase_ticket_goal_field, date_col, "tab" +
			goal_doctype_all[2], where_clause), as_dict=True)



	tour_goal_field = "base_cust_grand_total"

	sales_tour_results = frappe.db.sql('''
		select
			{0}({1}) as {1}, date_format({2}, '%m-%Y') as month_year
		from
			`{3}`
		{4}
		group by
			month_year'''.format(aggregation, tour_goal_field, date_col, "tab" +
			goal_doctype_all[3], where_clause), as_dict=True)

	purchase_tour_goal_field = "base_supp_grand_total"

	purchase_tour_results = frappe.db.sql('''
		select
			{0}({1}) as {1}, date_format({2}, '%m-%Y') as month_year
		from
			`{3}`
		{4}
		group by
			month_year'''.format(aggregation, purchase_tour_goal_field, date_col, "tab" +
			goal_doctype_all[3], where_clause), as_dict=True)

	month_to_value_sales_dict = {}
	for d in sales_results:
		month_to_value_sales_dict[d['month_year']] = d[goal_field]

	
	month_to_value_sales_ticket_dict = {}
	for d in sales_ticket_results:
		month_to_value_sales_ticket_dict[d['month_year']] = d[ticket_goal_field]

	month_to_value_sales_tour_dict = {}
	for d in sales_tour_results:
		month_to_value_sales_tour_dict[d['month_year']] = d[tour_goal_field]

	for d in sales_ticket_results:
		i = 0
		for r in sales_results:
			if d['month_year'] == r['month_year']:
				month_to_value_sales_dict[d['month_year']] = month_to_value_sales_dict[d['month_year']] \
					+ month_to_value_sales_ticket_dict[d['month_year']]
				i = 1
				break
		if i == 0:
			month_to_value_sales_dict[d['month_year']] = month_to_value_sales_ticket_dict[d['month_year']]

	for d in sales_tour_results:
		i = 0
		for r in sales_results:
			if d['month_year'] == r['month_year']:
				month_to_value_sales_dict[d['month_year']] = month_to_value_sales_dict[d['month_year']] \
					+ month_to_value_sales_tour_dict[d['month_year']]
				i = 1
				break
		if i == 0:
			if month_to_value_sales_dict[d['month_year']] == None:
				month_to_value_sales_dict[d['month_year']] = month_to_value_sales_tour_dict[d['month_year']]
			else:
				month_to_value_sales_dict[d['month_year']] = month_to_value_sales_dict[d['month_year']] \
					+ month_to_value_sales_tour_dict[d['month_year']]
				
	month_to_value_sales_dict_reversed = {}
	for d in month_to_value_sales_dict:
		d_reversed = d.split('-')[1] + "-" + d.split('-')[0]
		month_to_value_sales_dict_reversed[d_reversed] = month_to_value_sales_dict[d]

#	For python3.6+, the below can be used for sorting:
	month_to_value_sales_dict_reversed = dict(sorted(month_to_value_sales_dict_reversed.items()))

	month_to_value_sales_dict = {}
	for d in month_to_value_sales_dict_reversed:
		d_reversed = d.split('-')[1] + "-" + d.split('-')[0]
		month_to_value_sales_dict[d_reversed] = month_to_value_sales_dict_reversed[d]

	month_to_value_purchase_dict = {}
	for d in purchase_results:
		month_to_value_purchase_dict[d['month_year']] = d[goal_field]

	month_to_value_purchase_ticket_dict = {}
	for d in purchase_ticket_results:
		month_to_value_purchase_ticket_dict[d['month_year']] = d[purchase_ticket_goal_field]

	month_to_value_purchase_tour_dict = {}
	for d in purchase_tour_results:
		month_to_value_purchase_tour_dict[d['month_year']] = d[purchase_tour_goal_field]

	for d in purchase_ticket_results:
		i = 0
		for r in purchase_results:
			if d['month_year'] == r['month_year']:
				month_to_value_purchase_dict[d['month_year']] = month_to_value_purchase_dict[d['month_year']] \
					+ month_to_value_purchase_ticket_dict[d['month_year']]
				i = 1
				break
		if i == 0:
			month_to_value_purchase_dict[d['month_year']] = month_to_value_purchase_ticket_dict[d['month_year']]

	for d in purchase_tour_results:
		i = 0
		for r in purchase_results:
			if d['month_year'] == r['month_year']:
				month_to_value_purchase_dict[d['month_year']] = month_to_value_purchase_dict[d['month_year']] \
					+ month_to_value_purchase_tour_dict[d['month_year']]
				i = 1
				break
		if i == 0:
			if  month_to_value_purchase_dict[d['month_year']] == None:
				month_to_value_purchase_dict[d['month_year']] = month_to_value_purchase_tour_dict[d['month_year']]
			else:
				month_to_value_purchase_dict[d['month_year']] = month_to_value_purchase_dict[d['month_year']] \
					+month_to_value_purchase_tour_dict[d['month_year']]

	month_to_value_purchase_dict_reversed = {}
	for d in month_to_value_purchase_dict:
		d_reversed = d.split('-')[1] + "-" + d.split('-')[0]
		month_to_value_purchase_dict_reversed[d_reversed] = month_to_value_purchase_dict[d]

#	For python3.6+, the below can be used for sorting:
	month_to_value_purchase_dict_reversed = dict(sorted(month_to_value_purchase_dict_reversed.items()))

	month_to_value_purchase_dict = {}
	for d in month_to_value_purchase_dict_reversed:
		d_reversed = d.split('-')[1] + "-" + d.split('-')[0]
		month_to_value_purchase_dict[d_reversed] = month_to_value_purchase_dict_reversed[d]

	return month_to_value_sales_dict, month_to_value_purchase_dict


@frappe.whitelist()
def get_monthly_goal_graph_data(title, doctype, docname, goal_value_field, goal_total_field, goal_history_field,
	goal_doctype, goal_doctype_link, goal_field, date_field, filter_str, aggregation="sum"):
	'''
		Get month-wise graph data for a doctype based on aggregation values of a field in the goal doctype

		:param title: Graph title
		:param doctype: doctype of graph doc
		:param docname: of the doc to set the graph in
		:param goal_value_field: goal field of doctype
		:param goal_total_field: current month value field of doctype
		:param goal_history_field: cached history field
		:param goal_doctype: doctype the goal is based on
		:param goal_doctype_link: doctype link field in goal_doctype
		:param goal_field: field from which the goal is calculated
		:param filter_str: where clause condition
		:param aggregation: a value like 'count', 'sum', 'avg'

		:return: dict of graph data
	'''

	from frappe.utils.formatters import format_value
	import json

	meta = frappe.get_meta(doctype)
	doc = frappe.get_doc(doctype, docname)

	goal = doc.get(goal_value_field)
	formatted_goal = format_value(goal, meta.get_field(goal_value_field), doc)

	current_month_value = doc.get(goal_total_field)
	formatted_value = format_value(current_month_value, meta.get_field(goal_total_field), doc)

	current_month_purchase_value = doc.get("total_monthly_purchase")
	formatted_value = format_value(current_month_purchase_value, meta.get_field("total_monthly_purchase"), doc)

	from frappe.utils import today, getdate, formatdate, add_months
	current_month_year = formatdate(today(), "MM-yyyy")

	history = doc.get(goal_history_field)
	purchase_history = doc.get("purchase_monthly_history")
	try:
		month_to_value_dict = json.loads(history) if history and '{' in history else None
	except ValueError:
		month_to_value_dict = None

	try:
		month_to_value_purchase_dict = json.loads(purchase_history) if purchase_history and '{' in purchase_history else None
	except ValueError:
		month_to_value_purchase_dict = None

	month_to_value_dict = None
	month_to_value_purchase_dict = None
	if month_to_value_dict is None:
		doc_filter = (goal_doctype_link + ' = "' + docname + '"') if doctype != goal_doctype else ''
		if filter_str:
			doc_filter += ' and ' + filter_str if doc_filter else filter_str
		month_to_value_dict = get_monthly_results(goal_doctype, goal_field, date_field, doc_filter, aggregation)[0]
		frappe.db.set_value(doctype, docname, goal_history_field, json.dumps(month_to_value_dict))

	if month_to_value_purchase_dict is None:
		doc_filter = (goal_doctype_link + ' = "' + docname + '"') if doctype != goal_doctype else ''
		if filter_str:
			doc_filter += ' and ' + filter_str if doc_filter else filter_str
		month_to_value_purchase_dict = get_monthly_results(goal_doctype, goal_field, date_field, doc_filter, aggregation)[1]
		frappe.db.set_value(doctype, docname, "purchase_monthly_history", json.dumps(month_to_value_purchase_dict))


	month_to_value_dict[current_month_year] = current_month_value
	month_to_value_purchase_dict[current_month_year] = current_month_purchase_value

	months = []
	months_formatted = []
	values = []
	values_formatted = []
	purchase_values = []
	purchase_values_formatted = []
	for i in range(0, 12):
		date_value = add_months(today(), -i)
		month_value = formatdate(date_value, "MM-yyyy")
		month_word = getdate(date_value).strftime('%b')
		month_year = getdate(date_value).strftime('%B') + ', ' + getdate(date_value).strftime('%Y')
		months.insert(0, month_word)
		months_formatted.insert(0, month_year)
		if month_value in month_to_value_dict:
			val = month_to_value_dict[month_value]
		else:
			val = 0
		values.insert(0, val)
		values_formatted.insert(0, format_value(val, meta.get_field(goal_total_field), doc))


		if month_value in month_to_value_purchase_dict:
			val = month_to_value_purchase_dict[month_value]
		else:
			val = 0
		purchase_values.insert(0, val)
		purchase_values_formatted.insert(0, format_value(val, meta.get_field("total_monthly_purchase"), doc))

	y_markers = []
	summary_values = [
		{
			'title': _("This month"),
			'color': '#ffa00a',
			'value': formatted_value
		}
	]

	if float(goal) > 0:
		y_markers = [
			{
				'label': _("Goal"),
				'lineType': "dashed",
				'value': goal
			},
		]
		summary_values += [
			{
				'title': _("Goal"),
				'color': '#5e64ff',
				'value': formatted_goal
			},
			{
				'title': _("Completed"),
				'color': '#28a745',
				'value': str(int(round(float(current_month_value)/float(goal)*100))) + "%"
			}
		]

	data = {
		'title': title,
		# 'subtitle':

		'data': {
			'datasets': [
				{
					'name': "Sales",
					'values': values,
					'formatted': values_formatted
				},
				{
					'name': "Purchasing",
					'values': purchase_values,
#					'values': [0, 0, 0, 0, 0, 0, 0, 0, 0, 100.0, 1500.59, 3000.49] ,
					'formatted': purchase_values_formatted,
#					'formatted': ['$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 100.00', '$ 1,500.59', '$ 3,000.49']
				}
#				{
#					'name': "Expenses",
#					'values': [0, 0, 0, 0, 0, 0, 0, 0, 0, 10.0, 50.59, 1000.49] ,
#					'formatted': ['$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 0.00', '$ 10.00', '$ 50.59', '$1000.49']
#				}
			],
			'labels': months,
		},

		'summary': summary_values,
	}
        

	if y_markers:
		data["data"]["yMarkers"] = y_markers

	return data

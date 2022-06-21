from odoo import fields, models


class Company(models.Model):
    _inherit = 'res.company'

    ahkio_apix_transfer_id = fields.Char(string='Apix transfer ID')
    ahkio_apix_transfer_key = fields.Char(string='Apix transfer key')

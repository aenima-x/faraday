"""
Faraday Penetration Test IDE
Copyright (C) 2016  Infobyte LLC (https://faradaysec.com/)
See the file 'doc/LICENSE' for the license information
"""

# Standard library imports
import io
import logging
import json
from json.decoder import JSONDecodeError
from base64 import b64encode, b64decode
from pathlib import Path

# Related third party imports
import flask
from flask import request, send_file
from flask import Blueprint, make_response
from flask_classful import route
from filteralchemy import Filter, FilterSet, operators
from marshmallow import Schema, fields, post_load, ValidationError
from marshmallow.validate import OneOf
from sqlalchemy import desc, or_, func
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import aliased, joinedload, selectin_polymorphic, undefer, noload
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.datastructures import ImmutableMultiDict
from depot.manager import DepotManager

# Local application imports
from faraday.server.utils.search import search
from faraday.server.api.base import (
    AutoSchema,
    FilterAlchemyMixin,
    FilterSetMeta,
    PaginatedMixin,
    ReadWriteWorkspacedView,
    InvalidUsage,
    CountMultiWorkspacedMixin,
    BulkDeleteWorkspacedMixin,
    BulkUpdateWorkspacedMixin,
    get_filtered_data,
    parse_cve_cvss_references_and_policyviolations,
)
from faraday.server.api.modules.services import ServiceSchema
from faraday.server.fields import FaradayUploadedFile
from faraday.server.models import (
    db,
    File,
    Host,
    Service,
    Hostname,
    Workspace,
    Vulnerability,
    VulnerabilityWeb,
    CustomFieldsSchema,
    VulnerabilityGeneric,
    User
)
from faraday.server.utils.database import get_or_create
from faraday.server.utils.export import export_vulns_to_csv
from faraday.server.utils.filters import FlaskRestlessSchema
from faraday.server.schemas import (
    MutableField,
    SeverityField,
    MetadataSchema,
    SelfNestedField,
    FaradayCustomField,
    PrimaryKeyRelatedField,
)

vulns_api = Blueprint('vulns_api', __name__)
logger = logging.getLogger(__name__)


class EvidenceSchema(AutoSchema):
    content_type = fields.Method('get_content_type')
    data = fields.Method('get_data')

    class Meta:
        model = File
        fields = (
            'content_type',
            'data'
        )

    @staticmethod
    def get_content_type(file_obj):
        depot = DepotManager.get()
        return depot.get(file_obj.content.get('file_id')).content_type

    @staticmethod
    def get_data(file_obj):
        depot = DepotManager.get()
        return b64encode(depot.get(file_obj.content.get('file_id')).read()).decode()


class ImpactSchema(Schema):
    accountability = fields.Boolean(attribute='impact_accountability', default=False)
    availability = fields.Boolean(attribute='impact_availability', default=False)
    confidentiality = fields.Boolean(attribute='impact_confidentiality', default=False)
    integrity = fields.Boolean(attribute='impact_integrity', default=False)


class CustomMetadataSchema(MetadataSchema):
    """
    Implements command_id and creator logic
    """
    command_id = fields.Integer(dump_only=True, attribute='creator_command_id')
    creator = fields.Method('get_creator', dump_only=True)

    @staticmethod
    def get_creator(obj):
        if obj.tool:
            return obj.tool
        else:
            return obj.creator_command_tool or 'Web UI'


class CVESchema(AutoSchema):
    name = fields.String()


class VulnerabilitySchema(AutoSchema):
    _id = fields.Integer(dump_only=True, attribute='id')

    _rev = fields.String(dump_only=True, default='')
    _attachments = fields.Method(serialize='get_attachments', deserialize='load_attachments', default=[])
    owned = fields.Boolean(dump_only=True, default=False)
    owner = PrimaryKeyRelatedField('username', dump_only=True, attribute='creator')
    impact = SelfNestedField(ImpactSchema())
    desc = fields.String(attribute='description')
    description = fields.String(dump_only=True)
    policyviolations = fields.List(fields.String,
                                   attribute='policy_violations')
    refs = fields.List(fields.String(), attribute='references')
    owasp = fields.Method(serialize='get_owasp_refs', default=[])
    cve = fields.List(fields.String(), attribute='cve')
    cwe = fields.Method(serialize='get_cwe_refs', default=[])
    cvss = fields.Method(serialize='get_cvss_refs', default=[])
    issuetracker = fields.Method(serialize='get_issuetracker', dump_only=True)
    tool = fields.String(attribute='tool')
    parent = fields.Method(serialize='get_parent', deserialize='load_parent', required=True)
    parent_type = MutableField(fields.Method('get_parent_type'),
                               fields.String(),
                               required=True)
    tags = PrimaryKeyRelatedField('name', dump_only=True, many=True)
    easeofresolution = fields.String(
        attribute='ease_of_resolution',
        validate=OneOf(Vulnerability.EASE_OF_RESOLUTIONS),
        allow_none=True)
    hostnames = PrimaryKeyRelatedField('name', many=True, dump_only=True)
    service = fields.Nested(ServiceSchema(only=[
        '_id', 'ports', 'status', 'protocol', 'name', 'version', 'summary'
    ]), dump_only=True)
    host = fields.Integer(dump_only=True, attribute='host_id')
    severity = SeverityField(required=True)
    status = fields.Method(
        serialize='get_status',
        validate=OneOf(Vulnerability.STATUSES + ['opened']),
        deserialize='load_status')
    type = fields.Method(serialize='get_type',
                         deserialize='load_type',
                         required=True)
    obj_id = fields.String(dump_only=True, attribute='id')
    target = fields.String(dump_only=True, attribute='target_host_ip')
    host_os = fields.String(dump_only=True, attribute='target_host_os')
    metadata = SelfNestedField(CustomMetadataSchema())
    date = fields.DateTime(attribute='create_date',
                           dump_only=True)  # This is only used for sorting
    custom_fields = FaradayCustomField(table_name='vulnerability', attribute='custom_fields')
    external_id = fields.String(allow_none=True)

    class Meta:
        model = Vulnerability
        fields = (
            '_id', 'status',
            'issuetracker', 'description', 'parent', 'parent_type',
            'tags', 'severity', '_rev', 'easeofresolution', 'owned',
            'hostnames', 'owner',
            'date', 'data', 'refs',
            'desc', 'impact', 'confirmed', 'name',
            'service', 'obj_id', 'type', 'policyviolations', '_attachments',
            'target', 'host_os', 'resolution', 'metadata',
            'custom_fields', 'external_id', 'tool',
            'cvss', 'cwe', 'cve', 'owasp',
            )

    @staticmethod
    def get_type(obj):
        return obj.__class__.__name__

    @staticmethod
    def get_owasp_refs(obj):
        return [reference for reference in obj.references if 'owasp' in reference.lower()]

    @staticmethod
    def get_cwe_refs(obj):
        return [reference for reference in obj.references if 'cwe' in reference.lower()]

    @staticmethod
    def get_cvss_refs(obj):
        return [reference for reference in obj.references if 'cvss' in reference.lower()]

    @staticmethod
    def get_attachments(obj):
        res = {}

        for file_obj in obj.evidence:
            try:
                res[file_obj.filename] = EvidenceSchema().dump(file_obj)
            except OSError:
                logger.warning("File not found. Did you move your server?")

        return res

    @staticmethod
    def load_attachments(value):
        return value

    @staticmethod
    def get_parent(obj):
        return obj.service_id or obj.host_id

    @staticmethod
    def get_parent_type(obj):
        assert obj.service_id is not None or obj.host_id is not None
        return 'Service' if obj.service_id is not None else 'Host'

    @staticmethod
    def get_status(obj):
        return obj.status

    @staticmethod
    def get_issuetracker(obj):
        return {}

    @staticmethod
    def load_status(value):
        if value == 'opened':
            return 'open'
        return value

    @staticmethod
    def load_type(value):
        if value == 'Vulnerability':
            return 'vulnerability'
        if value == 'VulnerabilityWeb':
            return 'vulnerability_web'
        else:
            raise ValidationError('Invalid vulnerability type.')

    @staticmethod
    def load_parent(value):
        try:
            # sometimes api requests send str or unicode.
            value = int(value)
        except ValueError:

            raise ValidationError("Invalid parent type")
        return value

    @post_load
    def post_load_impact(self, data, **kwargs):
        # Unflatten impact (move data[impact][*] to data[*])
        impact = data.pop('impact', None)
        if impact:
            data.update(impact)
        return data

    @post_load
    def post_load_parent(self, data, **kwargs):
        # schema guarantees that parent_type exists.
        parent_class = None
        parent_field = None
        parent_type = data.pop('parent_type', None)
        parent_id = data.pop('parent', None)
        if not (parent_type and parent_id):
            # Probably a partial load, since they are required
            return data
        if parent_type == 'Host':
            parent_class = Host
            parent_field = 'host_id'
        if parent_type == 'Service':
            parent_class = Service
            parent_field = 'service_id'
        if not parent_class:
            raise ValidationError('Unknown parent type')
        if parent_type == 'Host' and data['type'] == 'vulnerability_web':
            raise ValidationError('Trying to set a host for a vulnerability web')

        try:
            parent = db.session.query(parent_class).join(Workspace).filter(
                Workspace.name == self.context['workspace_name'],
                parent_class.id == parent_id
            ).one()
        except NoResultFound:
            raise ValidationError(f'Parent id not found: {parent_id}')
        data[parent_field] = parent.id
        # TODO migration: check what happens when updating the parent from
        # service to host or viceverse
        return data


class VulnerabilityWebSchema(VulnerabilitySchema):
    method = fields.String(default='')
    params = fields.String(attribute='parameters', default='')
    pname = fields.String(attribute='parameter_name', default='')
    path = fields.String(default='')
    response = fields.String(default='')
    request = fields.String(default='')
    website = fields.String(default='')
    query = fields.String(attribute='query_string', default='')
    status_code = fields.Integer(allow_none=True)

    class Meta:
        model = VulnerabilityWeb
        fields = (
            '_id', 'status', 'parent_type',
            'website', 'issuetracker', 'description', 'parent',
            'tags', 'severity', '_rev', 'easeofresolution', 'owned',
            'hostnames', 'pname', 'query', 'owner',
            'path', 'date', 'data', 'response', 'refs',
            'desc', 'impact', 'confirmed', 'name',
            'service', 'obj_id', 'type', 'policyviolations',
            'request', '_attachments', 'params',
            'target', 'host_os', 'resolution', 'method', 'metadata',
            'status_code', 'custom_fields', 'external_id', 'tool',
            'cve', 'cwe', 'owasp', 'cvss',
        )


# Use this override for filterset fields that filter by en exact match by
# default, and not by a similar one (like operator)
_strict_filtering = {'default_operator': operators.Equal}


class IDFilter(Filter):
    def filter(self, query, model, attr, value):
        return query.filter(model.id == value)


class StatusCodeFilter(Filter):
    def filter(self, query, model, attr, value):
        return query.filter(model.status_code == value)


class TargetFilter(Filter):
    def filter(self, query, model, attr, value):
        return query.filter(model.target_host_ip == value)


class TypeFilter(Filter):
    def filter(self, query, model, attr, value):
        type_map = {
            'Vulnerability': 'vulnerability',
            'VulnerabilityWeb': 'vulnerability_web',
        }
        assert value in type_map
        return query.filter(model.__table__.c.type == type_map[value])


class CreatorFilter(Filter):
    def filter(self, query, model, attr, value):
        return query.filter(model.creator_command_tool.ilike(
            '%' + value + '%'))


class ServiceFilter(Filter):
    def filter(self, query, model, attr, value):
        alias = aliased(Service, name='service_filter')
        return query.join(
            alias,
            alias.id == model.__table__.c.service_id).filter(
            alias.name == value
        )


class HostnamesFilter(Filter):
    def filter(self, query, model, attr, value):
        alias = aliased(Hostname, name='hostname_filter')

        value_list = value.split(",")

        service_hostnames_query = query.join(Service, Service.id == Vulnerability.service_id). \
            join(Host). \
            join(alias). \
            filter(alias.name.in_(value_list))

        host_hostnames_query = query.join(Host, Host.id == Vulnerability.host_id). \
            join(alias). \
            filter(alias.name.in_(value_list))

        query = service_hostnames_query.union(host_hostnames_query)
        return query


class CustomILike(operators.Operator):
    """A filter operator that puts a % in the beginning and in the
    end of the search string to force a partial search"""

    def __call__(self, query, model, attr, value):
        column = getattr(model, attr)
        condition = column.ilike('%' + value + '%')
        return query.filter(condition)


class VulnerabilityFilterSet(FilterSet):
    class Meta(FilterSetMeta):
        model = VulnerabilityWeb  # It has all the fields
        # TODO migration: Check if we should add fields owner,
        # command, impact, issuetracker, tags, date, host
        # evidence, policy violations, hostnames

        fields = (
            "id", "status", "website", "parameter_name", "query_string", "path", "service",
            "data", "severity", "confirmed", "name", "request", "response",
            "parameters", "resolution",
            "description", "command_id", "target", "creator", "method",
            "ease_of_resolution", "service_id",
            "status_code", "tool",
        )

        strict_fields = (
            "severity", "confirmed", "method", "status", "ease_of_resolution",
            "service_id",
        )

        default_operator = CustomILike
        # next line uses dict comprehensions!
        column_overrides = {
            field: _strict_filtering for field in strict_fields
        }
        operators = (CustomILike, operators.Equal)

    id = IDFilter(fields.Int())
    target = TargetFilter(fields.Str())
    type = TypeFilter(fields.Str(validate=[OneOf(['Vulnerability',
                                                  'VulnerabilityWeb'])]))
    creator = CreatorFilter(fields.Str())
    service = ServiceFilter(fields.Str())
    severity = Filter(SeverityField())
    ease_of_resolution = Filter(fields.String(
        validate=OneOf(Vulnerability.EASE_OF_RESOLUTIONS),
        allow_none=True))
    status_code = StatusCodeFilter(fields.Int())
    status = Filter(fields.Function(
        deserialize=lambda val: 'open' if val == 'opened' else val,
        validate=OneOf(Vulnerability.STATUSES + ['opened'])
    ))
    hostnames = HostnamesFilter(fields.Str())
    confirmed = Filter(fields.Boolean())

    def filter(self):
        """Generate a filtered query from request parameters.

        :returns: Filtered SQLAlchemy query
        """
        # TODO migration: this can became a normal filter instead of a custom
        # one, since now we can use creator_command_id
        command_id = request.args.get('command_id')

        # The web UI uses old field names. Translate them into the new field
        # names to maintain backwards compatibility
        param_mapping = {
            'query': 'query_string',
            'pname': 'parameter_name',
            'params': 'parameters',
            'easeofresolution': 'ease_of_resolution',
        }
        new_args = request.args.copy()
        for (old_param, real_param) in param_mapping.items():
            try:
                new_args[real_param] = request.args[old_param]
            except KeyError:
                pass
        request.args = ImmutableMultiDict(new_args)

        query = super().filter()

        if command_id:
            # query = query.filter(CommandObject.command_id == int(command_id))
            query = query.filter(VulnerabilityGeneric.creator_command_id
                                 == int(command_id))  # TODO migration: handle invalid int()
        return query


class VulnerabilityView(PaginatedMixin,
                        FilterAlchemyMixin,
                        ReadWriteWorkspacedView,
                        CountMultiWorkspacedMixin,
                        BulkDeleteWorkspacedMixin,
                        BulkUpdateWorkspacedMixin):
    route_base = 'vulns'
    filterset_class = VulnerabilityFilterSet
    sort_model_class = VulnerabilityWeb  # It has all the fields
    sort_pass_silently = True  # For compatibility with the Web UI
    order_field = desc(VulnerabilityGeneric.confirmed), VulnerabilityGeneric.severity, VulnerabilityGeneric.create_date
    get_joinedloads = [Vulnerability.evidence, Vulnerability.creator]

    model_class_dict = {
        'Vulnerability': Vulnerability,
        'VulnerabilityWeb': VulnerabilityWeb,
        'VulnerabilityGeneric': VulnerabilityGeneric,  # For listing objects
    }
    schema_class_dict = {
        'Vulnerability': VulnerabilitySchema,
        'VulnerabilityWeb': VulnerabilityWebSchema
    }

    def _get_schema_instance(self, route_kwargs, **kwargs):
        schema = super()._get_schema_instance(route_kwargs, **kwargs)

        return schema

    def _perform_create(self, data, **kwargs):
        data = self._parse_data(self._get_schema_instance(kwargs), request)
        obj = None
        # TODO migration: use default values when popping and validate the
        # popped object has the expected type.
        # This will be set after setting the workspace
        attachments = data.pop('_attachments', {})
        references = data.pop('references', [])
        policyviolations = data.pop('policy_violations', [])
        cve_list = data.pop('cve', [])

        try:
            obj = super()._perform_create(data, **kwargs)
        except TypeError:
            # TypeError is raised when trying to instantiate an sqlalchemy model
            # with invalid attributes, for example VulnerabilityWeb with host_id
            flask.abort(400)

        obj = parse_cve_cvss_references_and_policyviolations(obj, references, policyviolations,
                                                             cve_list)

        db.session.flush()

        self._process_attachments(obj, attachments)
        if not obj.tool:
            if obj.creator_command_tool:
                obj.tool = obj.creator_command_tool
            else:
                obj.tool = "Web UI"
        db.session.commit()
        return obj

    @staticmethod
    def _process_attachments(obj, attachments):
        old_attachments = db.session.query(File).options(
            joinedload(File.creator),
            joinedload(File.update_user)
        ).filter_by(
            object_id=obj.id,
            object_type='vulnerability',
        )
        for old_attachment in old_attachments:
            db.session.delete(old_attachment)
        for filename, attachment in attachments.items():
            faraday_file = FaradayUploadedFile(b64decode(attachment['data']))
            filename = filename.replace(" ", "_")
            get_or_create(
                db.session,
                File,
                object_id=obj.id,
                object_type='vulnerability',
                name=Path(filename).stem,
                filename=Path(filename).name,
                content=faraday_file,
            )

    def _update_object(self, obj, data, **kwargs):
        data.pop('type', '')  # It's forbidden to change vuln type!
        data.pop('tool', '')

        return super()._update_object(obj, data)

    def _perform_update(self, object_id, obj, data, workspace_name=None, partial=False):
        attachments = data.pop('_attachments', None if partial else {})
        obj = super()._perform_update(object_id, obj, data, workspace_name)
        db.session.flush()
        if attachments is not None:
            self._process_attachments(obj, attachments)
        db.session.commit()
        return obj

    def _get_eagerloaded_query(self, *args, **kwargs):
        """Eager hostnames loading.

        This is too complex to get_joinedloads so I have to
        override the function
        """
        query = super()._get_eagerloaded_query(
            *args, **kwargs)
        options = [
            joinedload(Vulnerability.host).
            load_only(Host.id).  # Only hostnames are needed
            joinedload(Host.hostnames),

            joinedload(Vulnerability.service).
            joinedload(Service.host).
            joinedload(Host.hostnames),

            joinedload(VulnerabilityWeb.service).
            joinedload(Service.host).
            joinedload(Host.hostnames),

            joinedload(VulnerabilityGeneric.update_user),
            undefer(VulnerabilityGeneric.creator_command_id),
            undefer(VulnerabilityGeneric.creator_command_tool),
            undefer(VulnerabilityGeneric.target_host_ip),
            undefer(VulnerabilityGeneric.target_host_os),
            joinedload(VulnerabilityGeneric.tags),
        ]

        if flask.request.args.get('get_evidence'):
            options.append(joinedload(VulnerabilityGeneric.evidence))
        else:
            options.append(noload(VulnerabilityGeneric.evidence))

        return query.options(selectin_polymorphic(
            VulnerabilityGeneric,
            [Vulnerability, VulnerabilityWeb]
        ), *options)

    def _filter_query(self, query):
        query = super()._filter_query(query)
        search_term = flask.request.args.get('search', None)
        if search_term is not None:
            # TODO migration: add more fields to free text search
            like_term = '%' + search_term + '%'
            match_name = VulnerabilityGeneric.name.ilike(like_term)
            match_desc = VulnerabilityGeneric.description.ilike(like_term)
            query = query.filter(match_name | match_desc)
        return query

    @property
    def model_class(self):
        if request.method == 'POST':
            return self.model_class_dict[request.json['type']]
        # We use Generic to list all vulns from all types
        return self.model_class_dict['VulnerabilityGeneric']

    def _get_schema_class(self):
        assert self.schema_class_dict is not None, "You must define schema_class"
        if request.method == 'POST' and request.json:
            requested_type = request.json.get('type', None)
            if not requested_type:
                raise InvalidUsage('Type is required.')
            if requested_type not in self.schema_class_dict:
                raise InvalidUsage('Invalid vulnerability type.')
            return self.schema_class_dict[requested_type]
        # We use web since it has all the fields
        return self.schema_class_dict['VulnerabilityWeb']

    def _envelope_list(self, objects, pagination_metadata=None):
        vulns = []
        for index, vuln in enumerate(objects):
            # we use index when the filter endpoint uses group by and
            # the _id was not used in the group by
            vulns.append({
                'id': vuln.get('_id', index),
                'key': vuln.get('_id', index),
                'value': vuln
            })
        return {
            'vulnerabilities': vulns,
            'count': (pagination_metadata.total
                      if pagination_metadata is not None else len(vulns))
        }

    def count(self, **kwargs):
        """
        ---
        get:
          tags: ["Vulnerability"]
          summary: "Group vulnerabilities by the field set in the group_by GET parameter."
          responses:
            200:
              description: Ok
              content:
                application/json:
                  schema: VulnerabilityWeb
            404:
              description: group_by is not specified
        tags: ["Vulnerability"]
        responses:
          200:
            description: Ok
        """
        res = super().count(**kwargs)

        def convert_group(group):
            group = group.copy()
            severity_map = {
                "informational": "info",
                "medium": "med"
            }
            severity = group['severity']
            group['severity'] = group['name'] = severity_map.get(
                severity, severity)
            return group

        if request.args.get('group_by') == 'severity':
            res['groups'] = [convert_group(group) for group in res['groups']]
        return res

    @route('/<int:vuln_id>/attachment', methods=['POST'])
    def post_attachment(self, workspace_name, vuln_id):
        """
        ---
        post:
          tags: ["Vulnerability", "File"]
          description: Creates a new attachment in the vuln
          responses:
            201:
              description: Created
        tags: ["Vulnerability", "File"]
        responses:
          200:
            description: Ok
        """

        vuln_workspace_check = db.session.query(VulnerabilityGeneric, Workspace.id).join(
            Workspace).filter(VulnerabilityGeneric.id == vuln_id,
                              Workspace.name == workspace_name).first()

        if vuln_workspace_check:
            if 'file' not in request.files:
                flask.abort(400)
            vuln = VulnerabilitySchema().dump(vuln_workspace_check[0])
            filename = request.files['file'].filename
            _attachments = vuln['_attachments']
            if filename in _attachments:
                message = 'Evidence already exists in vuln'
                return make_response(flask.jsonify(message=message, success=False, code=400), 400)
            else:
                faraday_file = FaradayUploadedFile(request.files['file'].read())
                instance, created = get_or_create(
                    db.session,
                    File,
                    object_id=vuln_id,
                    object_type='vulnerability',
                    name=filename,
                    filename=filename,
                    content=faraday_file
                )
                db.session.commit()
                message = 'Evidence upload was successful'
                logger.info(message)
                return flask.jsonify({'message': message})
        else:
            flask.abort(404, "Vulnerability not found")

    @route('/filter')
    def filter(self, workspace_name):
        """
        ---
        get:
          tags: ["Filter", "Vulnerability"]
          description: Filters, sorts and groups vulnerabilities using a json with parameters. These parameters must be part of the model.
          parameters:
          - in: query
            name: q
            description: Recursive json with filters that supports operators. The json could also contain sort and group.
          responses:
            200:
              description: Returns filtered, sorted and grouped results
              content:
                application/json:
                  schema: FlaskRestlessSchema
            400:
              description: Invalid q was sent to the server
        tags: ["Filter", "Vulnerability"]
        responses:
          200:
            description: Ok
        """
        filters = request.args.get('q', '{}')
        filtered_vulns, count = self._filter(filters, workspace_name)
        export_csv = request.args.get('export_csv', '')

        class PageMeta:
            total = 0

        pagination_metadata = PageMeta()
        pagination_metadata.total = count
        if export_csv.lower() == 'true':
            custom_fields_columns = []
            for custom_field in db.session.query(CustomFieldsSchema).order_by(CustomFieldsSchema.field_order):
                custom_fields_columns.append(custom_field.field_name)
            memory_file = export_vulns_to_csv(filtered_vulns, custom_fields_columns)
            return send_file(memory_file,
                             attachment_filename=f"Faraday-SR-{workspace_name}.csv",
                             as_attachment=True,
                             cache_timeout=-1)
        else:
            return self._envelope_list(filtered_vulns, pagination_metadata)

    def _hostname_filters(self, filters):
        res_filters = []
        hostname_filters = []
        for search_filter in filters:
            if 'or' not in search_filter and 'and' not in search_filter:
                fieldname = search_filter.get('name')
                operator = search_filter.get('op')
                argument = search_filter.get('val')
                otherfield = search_filter.get('field')
                field_filter = {
                    "name": fieldname,
                    "op": operator,
                    "val": argument,

                }
                if otherfield:
                    field_filter.update({"field": otherfield})
                if fieldname == 'hostnames':
                    hostname_filters.append(field_filter)
                else:
                    res_filters.append(field_filter)
            elif 'or' in search_filter:
                or_filters, deep_hostname_filters = self._hostname_filters(search_filter['or'])
                if or_filters:
                    res_filters.append({"or": or_filters})
                hostname_filters += deep_hostname_filters
            elif 'and' in search_filter:
                and_filters, deep_hostname_filters = self._hostname_filters(search_filter['and'])
                if and_filters:
                    res_filters.append({"and": and_filters})
                hostname_filters += deep_hostname_filters

        return res_filters, hostname_filters

    @staticmethod
    def _generate_filter_query(vulnerability_class, filters, hostname_filters, workspace, marshmallow_params):
        hosts_os_filter = [host_os_filter for host_os_filter in filters.get('filters', []) if
                           host_os_filter.get('name') == 'host__os']

        if hosts_os_filter:
            # remove host__os filters from filters due to a bug
            hosts_os_filter = hosts_os_filter[0]
            filters['filters'] = [host_os_filter for host_os_filter in filters.get('filters', []) if
                                  host_os_filter.get('name') != 'host__os']

        vulns = search(db.session,
                       vulnerability_class,
                       filters)
        vulns = vulns.filter(VulnerabilityGeneric.workspace == workspace)
        if hostname_filters:
            or_filters = []
            for hostname_filter in hostname_filters:
                or_filters.append(Hostname.name == hostname_filter['val'])

            vulns_host = vulns.join(Host).join(Hostname).filter(or_(*or_filters))
            vulns = vulns_host.union(
                vulns.join(Service).join(Host).join(Hostname).filter(or_(*or_filters)))

        if hosts_os_filter:
            os_value = hosts_os_filter['val']
            vulns = vulns.join(Host).join(Service).filter(Host.os == os_value)

        if 'group_by' not in filters:
            vulns = vulns.options(
                joinedload(VulnerabilityGeneric.tags),
                joinedload(Vulnerability.host),
                joinedload(Vulnerability.service),
                joinedload(VulnerabilityWeb.service),
            )
        return vulns

    def _filter(self, filters, workspace_name):
        hostname_filters = []
        vulns = None
        try:
            filters = FlaskRestlessSchema().load(json.loads(filters)) or {}
            if filters:
                filters['filters'], hostname_filters = self._hostname_filters(filters.get('filters', []))
        except (ValidationError, JSONDecodeError) as ex:
            logger.exception(ex)
            flask.abort(400, "Invalid filters")

        workspace = self._get_workspace(workspace_name)
        marshmallow_params = {'many': True, 'context': {}, 'exclude': ('_attachments', )}
        if 'group_by' not in filters:
            offset = None
            limit = None
            if 'offset' in filters:
                offset = filters.pop('offset')
            if 'limit' in filters:
                limit = filters.pop('limit')  # we need to remove pagination, since

            try:
                vulns = self._generate_filter_query(
                    VulnerabilityGeneric,
                    filters,
                    hostname_filters,
                    workspace,
                    marshmallow_params)
            except AttributeError as e:
                flask.abort(400, e)
            total_vulns = vulns
            if limit:
                vulns = vulns.limit(limit)
            if offset:
                vulns = vulns.offset(offset)

            vulns = self.schema_class_dict['VulnerabilityWeb'](**marshmallow_params).dump(vulns)
            return vulns, total_vulns.count()
        else:
            vulns = self._generate_filter_query(
                VulnerabilityGeneric,
                filters,
                hostname_filters,
                workspace,
                marshmallow_params,
            )
            vulns_data, rows_count = get_filtered_data(filters, vulns)

            return vulns_data, rows_count

    @route('/<int:vuln_id>/attachment/<attachment_filename>', methods=['GET'])
    def get_attachment(self, workspace_name, vuln_id, attachment_filename):
        """
        ---
        get:
          tags: ["Vulnerability", "File"]
          description: Get a vuln attachment
          responses:
            200:
              description: Ok
        tags: ["Vulnerability", "File"]
        responses:
          200:
            description: Ok
        """
        vuln_workspace_check = db.session.query(VulnerabilityGeneric, Workspace.id).join(
            Workspace).filter(VulnerabilityGeneric.id == vuln_id,
                              Workspace.name == workspace_name).first()

        if vuln_workspace_check:
            file_obj = db.session.query(File).filter_by(object_type='vulnerability',
                                                        object_id=vuln_id,
                                                        filename=attachment_filename.replace(" ", "%20")).first()
            if file_obj:
                depot = DepotManager.get()
                depot_file = depot.get(file_obj.content.get('file_id'))
                if depot_file.content_type.startswith('image/'):
                    # Image content types are safe (they can't be executed like
                    # html) so we don't have to force the download of the file
                    as_attachment = False
                else:
                    as_attachment = True
                return flask.send_file(
                    io.BytesIO(depot_file.read()),
                    attachment_filename=file_obj.filename,
                    as_attachment=as_attachment,
                    mimetype=depot_file.content_type
                )
            else:
                flask.abort(404, "File not found")
        else:
            flask.abort(404, "Vulnerability not found")

    @route('/<int:vuln_id>/attachment', methods=['GET'])
    def get_attachments_by_vuln(self, workspace_name, vuln_id):
        """
        ---
        get:
          tags: ["Vulnerability", "File"]
          description: Gets an attachment for a vulnerability
          responses:
            200:
              description: Ok
              content:
                application/json:
                  schema: EvidenceSchema
            403:
              description: Workspace disabled or no permission
            404:
              description: Not Found
        tags: ["Vulnerability", "File"]
        responses:
          200:
            description: Ok
        """
        workspace = self._get_workspace(workspace_name)
        vuln_workspace_check = db.session.query(VulnerabilityGeneric, Workspace.id).join(
            Workspace).filter(VulnerabilityGeneric.id == vuln_id,
                              Workspace.name == workspace.name).first()
        if vuln_workspace_check:
            files = db.session.query(File).filter_by(object_type='vulnerability',
                                                     object_id=vuln_id).all()
            res = {}
            for file_obj in files:
                ret = EvidenceSchema().dump(file_obj)
                res[file_obj.filename] = ret

            return flask.jsonify(res)
        else:
            flask.abort(404, "Vulnerability not found")

    @route('/<int:vuln_id>/attachment/<attachment_filename>', methods=['DELETE'])
    def delete_attachment(self, workspace_name, vuln_id, attachment_filename):
        """
        ---
        delete:
          tags: ["Vulnerability", "File"]
          description: Remove a vuln attachment
          responses:
            200:
              description: Ok
        """
        vuln_workspace_check = db.session.query(VulnerabilityGeneric, Workspace.id).join(
            Workspace).filter(
            VulnerabilityGeneric.id == vuln_id, Workspace.name == workspace_name).first()

        if vuln_workspace_check:
            file_obj = db.session.query(File).filter_by(object_type='vulnerability',
                                                        object_id=vuln_id,
                                                        filename=attachment_filename).first()
            if file_obj:
                db.session.delete(file_obj)
                db.session.commit()
                depot = DepotManager.get()
                depot.delete(file_obj.content.get('file_id'))
                message = 'Attachment was successfully deleted'
                logger.info(message)
                return flask.jsonify({'message': message})
            else:
                flask.abort(404, "File not found")
        else:
            flask.abort(404, "Vulnerability not found")

    @route('export_csv', methods=['GET'])
    def export_csv(self, workspace_name):
        """
        ---
        get:
          tags: ["Vulnerability", "File"]
          description: Get a CSV file with all vulns from a workspace
          responses:
            200:
              description: Ok
        tags: ["Vulnerability", "File"]
        responses:
          200:
            description: Ok
        """
        confirmed = bool(request.args.get('confirmed'))
        filters = request.args.get('q', '{}')
        custom_fields_columns = []
        for custom_field in db.session.query(CustomFieldsSchema).order_by(CustomFieldsSchema.field_order):
            custom_fields_columns.append(custom_field.field_name)
        if confirmed:
            if 'filters' not in filters:
                filters = {'filters': []}
            filters['filters'].append({
                "name": "confirmed",
                "op": "==",
                "val": "true"
            })
            filters = json.dumps(filters)
        vulns_query, _ = self._filter(filters, workspace_name)
        memory_file = export_vulns_to_csv(vulns_query, custom_fields_columns)
        logger.info(f"csv file with vulns from workspace {workspace_name} exported")
        return send_file(memory_file,
                         attachment_filename=f"Faraday-SR-{workspace_name}.csv",
                         as_attachment=True,
                         cache_timeout=-1)

    @route('top_users', methods=['GET'])
    def top_users(self, workspace_name):
        """
        ---
        get:
          tags: ["Vulnerability"]
          params: limit
          description: Gets a list of top users having account its uploaded vulns
          responses:
            200:
              description: List of top users
        tags: ["Vulnerability"]
        responses:
          200:
            description: Ok
        """
        limit = flask.request.args.get('limit', 1)
        workspace = self._get_workspace(workspace_name)
        data = db.session.query(User, func.count(VulnerabilityGeneric.id)).join(VulnerabilityGeneric.creator) \
            .filter(VulnerabilityGeneric.workspace_id == workspace.id).group_by(User.id) \
            .order_by(desc(func.count(VulnerabilityGeneric.id))).limit(int(limit)).all()
        users = []
        for item in data:
            user = {
                'id': item[0].id,
                'username': item[0].username,
                'count': item[1]
            }
            users.append(user)
        response = {'users': users}
        return flask.jsonify(response)

    @route('', methods=['DELETE'])
    def bulk_delete(self, workspace_name, **kwargs):
        # TODO BULK_DELETE_SCHEMA
        if not flask.request.json or 'severities' not in flask.request.json:
            return BulkDeleteWorkspacedMixin.bulk_delete(self, workspace_name, **kwargs)
        return self._perform_bulk_delete(flask.request.json['severities'], by='severity',
                                         workspace_name=workspace_name, **kwargs), 200
    bulk_delete.__doc__ = BulkDeleteWorkspacedMixin.bulk_delete.__doc__

    def _bulk_update_query(self, ids, **kwargs):
        # It IS better to as is but warn of ON CASCADE
        query = self.model_class.query.filter(self.model_class.id.in_(ids))
        workspace = self._get_workspace(kwargs.pop("workspace_name"))
        return query.filter(self.model_class.workspace_id == workspace.id)

    def _bulk_delete_query(self, ids, **kwargs):
        # It IS better to as is but warn of ON CASCADE
        if kwargs.get("by", "id") != "severity":
            query = self.model_class.query.filter(self.model_class.id.in_(ids))
        else:
            query = self.model_class.query.filter(self.model_class.severity.in_(ids))
        workspace = self._get_workspace(kwargs.pop("workspace_name"))
        return query.filter(self.model_class.workspace_id == workspace.id)

    def _get_model_association_proxy_fields(self):
        return [
            field.target_collection
            for field in inspect(self.model_class).all_orm_descriptors
            if field.extension_type.name == "ASSOCIATION_PROXY"
        ]

    def _pre_bulk_update(self, data, **kwargs):
        data.pop('type', '')  # It's forbidden to change vuln type!
        data.pop('tool', '')
        data.pop('service_id', '')
        data.pop('host_id', '')

        # TODO For now, we don't want to accept multiples attachments; moreover, attachments have its own endpoint
        data.pop('_attachments', [])
        super()._pre_bulk_update(data, **kwargs)

        model_association_proxy_fields = self._get_model_association_proxy_fields()
        association_proxy_fields = {}
        for key in list(data):
            parent = getattr(VulnerabilityWeb, key).parent
            field_name = getattr(parent, "target_collection", None)
            if field_name and field_name in model_association_proxy_fields:
                association_proxy_fields[key] = data.pop(key)
        return association_proxy_fields

    def _post_bulk_update(self, ids, extracted_data, workspace_name, **kwargs):
        if extracted_data:
            queryset = self._bulk_update_query(
                                               ids,
                                               workspace_name=workspace_name,
                                               **kwargs)
            for obj in queryset.all():
                for (key, value) in extracted_data.items():
                    setattr(obj, key, value)
                    db.session.add(obj)


VulnerabilityView.register(vulns_api)

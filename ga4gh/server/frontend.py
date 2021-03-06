"""
The Flask frontend for the GA4GH API.

TODO Document properly.
"""
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import datetime
import time
import socket
import urlparse
import functools
import json

import flask
from flask_cors import cross_origin, CORS
# from flask.ext.oidc import OpenIDConnect
from flask import jsonify, render_template, request  # Flask
import humanize
import werkzeug
import oic
# from oic.oic import AuthorizationRequest, Client
import oic.oauth2
import oic.oic.message as message
# from oic.utils.http_util import Redirect
# from oauth2client.client import OAuth2Credentials
import requests
import logging
from logging import StreamHandler
from werkzeug.contrib.cache import FileSystemCache

import ga4gh.server
import ga4gh.server.backend as backend
import ga4gh.server.datamodel as datamodel
import ga4gh.server.exceptions as exceptions
import ga4gh.server.datarepo as datarepo
import ga4gh.server.auth as auth
import ga4gh.server.network as network
# import ga4gh.server.DP as DP
import ga4gh.server.NCIT as NCIT

import ga4gh.schemas.protocol as protocol

from ga4gh.client import client
import base64

SEARCH_ENDPOINT_METHODS = ['POST', 'OPTIONS']
SECRET_KEY_LENGTH = 24
LOGIN_ENDPOINT_METHODS = ['GET', 'POST']

app = flask.Flask(__name__)
assert not hasattr(app, 'urls')
app.urls = []
app.url_map.strict_slashes = False

requires_auth = auth.auth_decorator(app)


class NoConverter(werkzeug.routing.BaseConverter):
    """
    A converter that allows the routing matching algorithm to not
    match on certain literal terms

    This is needed because if there are e.g. two routes:

    /callsets/search
    /callsets/<id>

    A request for /callsets/search will get routed to
    the second, which is not what we want.
    """
    def __init__(self, map, *items):
        werkzeug.routing.BaseConverter.__init__(self, map)
        self.items = items

    def to_python(self, value):
        if value in self.items:
            raise werkzeug.routing.ValidationError()
        return value


app.url_map.converters['no'] = NoConverter


class ServerStatus(object):
    """
    Generates information about the status of the server for display
    """
    def __init__(self):
        self.startupTime = datetime.datetime.now()

    def getConfiguration(self):
        """
        Returns a list of configuration (key, value) tuples
        that are useful for users to view on an information page.
        Note that we should be careful here not to leak sensitive
        information. For example, keys and paths of data files should
        not be returned.
        """
        # TODO what other config keys are appropriate to export here?
        keys = [
            'DEBUG', 'REQUEST_VALIDATION',
            'DEFAULT_PAGE_SIZE', 'MAX_RESPONSE_LENGTH', 'LANDING_MESSAGE_HTML'
        ]
        return [(k, app.config[k]) for k in keys]

    def getPreciseUptime(self):
        """
        Returns the server precisely.
        """
        return self.startupTime.strftime("%H:%M:%S %d %b %Y")

    def getLandingMessageHtml(self):
        filePath = app.config.get('LANDING_MESSAGE_HTML')
        try:
            htmlFile = open(filePath, 'r')
            html = htmlFile.read()
            htmlFile.close()
        except:
            html = flask.render_template("landing_message.html")
        return html

    def getNaturalUptime(self):
        """
        Returns the uptime in a human-readable format.
        """
        return humanize.naturaltime(self.startupTime)

    def getProtocolVersion(self):
        """
        Returns the GA4GH protocol version we support.
        """
        return protocol.version

    def getServerVersion(self):
        """
        Returns the software version of this server.
        """
        return ga4gh.server.__version__

    def getUrls(self):
        """
        Returns the list of (httpMethod, URL) tuples that this server
        supports.
        """
        app.urls.sort()
        return app.urls

    def getDatasets(self):
        """
        Returns the list of datasetIds for this backend
        """
        return app.backend.getDataRepository().getDatasets()

    def getExperiments(self):
        """
        Returns the list of experimentIds for this backend
        """
        return app.backend.getDataRepository().getExperiments()

    def getAnalyses(self):
        """
        Returns the list of analysisIds for this backend
        """
        return app.backend.getDataRepository().getAnalyses()

    def getVariantSets(self, datasetId):
        """
        Returns the list of variant sets for the dataset
        """
        return app.backend.getDataRepository().getDataset(
            datasetId).getVariantSets()

    def getFeatureSets(self, datasetId):
        """
        Returns the list of feature sets for the dataset
        """
        return app.backend.getDataRepository().getDataset(
            datasetId).getFeatureSets()

    def getContinuousSets(self, datasetId):
        """
        Returns the list of continuous sets for the dataset
        """
        return app.backend.getDataRepository().getDataset(
            datasetId).getContinuousSets()

    def getReadGroupSets(self, datasetId):
        """
        Returns the list of ReadGroupSets for the dataset
        """
        return app.backend.getDataRepository().getDataset(
            datasetId).getReadGroupSets()

    def getReferenceSets(self):
        """
        Returns the list of ReferenceSets for this server.
        """
        return app.backend.getDataRepository().getReferenceSets()

    def getVariantAnnotationSets(self, datasetId):
        """
        Returns the list of ReferenceSets for this server.
        """
        # TODO this should be displayed per-variant set, not per dataset.
        variantAnnotationSets = []
        dataset = app.backend.getDataRepository().getDataset(datasetId)
        for variantSet in dataset.getVariantSets():
            variantAnnotationSets.extend(
                variantSet.getVariantAnnotationSets())
        return variantAnnotationSets

    def getPhenotypeAssociationSets(self, datasetId):
        return app.backend.getDataRepository().getDataset(
            datasetId).getPhenotypeAssociationSets()

    def getRnaQuantificationSets(self, datasetId):
        """
        Returns the list of RnaQuantificationSets for this server.
        """
        return app.backend.getDataRepository().getDataset(
            datasetId).getRnaQuantificationSets()

    def getPeers(self):
        """
        Returns the list of peers.
        """
        return app.backend.getDataRepository().getPeers()

    def getOntologyByName(self, name):
        """
        Returns the list of ontologies.
        """
        return app.backend.getDataRepository().getOntologyByName(name)


class UserAccessMap(object):
    """
    Loads local authorization info from an access list file to the backend

    access_list.txt formatting example:
    username1:project1:0
    username2:project2:4
    """
    def __init__(self):
        self.user_access_map = {}
        self.file_path = app.config.get('ACCESS_LIST')

        if not self.file_path:
            raise exceptions.ConfigurationException("No user access list defined")

        self.list_updated = os.path.getmtime(self.file_path)

    def initializeUserAccess(self, logger=None):
        try:
            with open(self.file_path) as access_list:
                for line in access_list:
                    parsed_line = line.split(":")
                    if len(parsed_line) != 3:
                        raise exceptions.MalformedAccessException
                    username = parsed_line[0]
                    project = parsed_line[1]
                    level = parsed_line[2].strip('\n')
                    try:
                        int(level)
                    except ValueError:
                        exceptions.MalformedAccessException("Non-integer access level provided")
                    if username not in self.user_access_map:
                        self.user_access_map[username] = {}
                    if any(project in role for role in self.user_access_map[username]):
                        if logger:
                            logger.warn(
                                "Duplicate roles detected for {}. Resolve in access list".format(username)
                            )
                    else:
                        self.user_access_map[username][project] = level
        except:
            raise exceptions.MalformedAccessException

    def getUserAccessMap(self):
        return self.user_access_map

    def getFilePath(self):
        return self.file_path

    def getListUpdated(self):
        return self.list_updated


def load_access_map():
    """
    Reloads the user access map from file
    """
    app.access_map = UserAccessMap()
    app.access_map.initializeUserAccess(app.logger)


def render_candig_template(template_path, **kwargs):
    """
    wrapper for flask render template to apply preset environment variables

    :param template_path: template file to render
    :param kwargs: additional variables
    :return: call to flask.render_template
    """
    return flask.render_template(
        template_path,
        session_id=flask.session.get('id_token', ''),
        prepend_path=app.config.get('TYK_LISTEN_PATH', ''),
        logout_url=_generate_logout_url(),
        username=flask.session.get('username', 'N/A'),
        **kwargs
    )


def reset():
    """
    Resets the flask app; used in testing
    """
    app.config.clear()
    configStr = 'ga4gh.server.serverconfig:FlaskDefaultConfig'
    app.config.from_object(configStr)


def _configure_backend(app):
    """A helper function used just to help modularize the code a bit."""
    # Allocate the backend
    # We use URLs to specify the backend. Currently we have file:// URLs (or
    # URLs with no scheme) for the SqlDataRepository, and special empty:// and
    # simulated:// URLs for empty or simulated data sources.
    dataSource = urlparse.urlparse(app.config["DATA_SOURCE"], "file")

    if dataSource.scheme == "simulated":
        # Ignore the query string
        randomSeed = app.config["SIMULATED_BACKEND_RANDOM_SEED"]
        numCalls = app.config["SIMULATED_BACKEND_NUM_CALLS"]
        variantDensity = app.config["SIMULATED_BACKEND_VARIANT_DENSITY"]
        numVariantSets = app.config["SIMULATED_BACKEND_NUM_VARIANT_SETS"]
        numReferenceSets = app.config[
            "SIMULATED_BACKEND_NUM_REFERENCE_SETS"]
        numReferencesPerReferenceSet = app.config[
            "SIMULATED_BACKEND_NUM_REFERENCES_PER_REFERENCE_SET"]
        numAlignmentsPerReadGroup = app.config[
            "SIMULATED_BACKEND_NUM_ALIGNMENTS_PER_READ_GROUP"]
        numReadGroupsPerReadGroupSet = app.config[
            "SIMULATED_BACKEND_NUM_READ_GROUPS_PER_READ_GROUP_SET"]
        numPhenotypeAssociations = app.config[
            "SIMULATED_BACKEND_NUM_PHENOTYPE_ASSOCIATIONS"]
        numPhenotypeAssociationSets = app.config[
            "SIMULATED_BACKEND_NUM_PHENOTYPE_ASSOCIATION_SETS"]
        numRnaQuantSets = app.config[
            "SIMULATED_BACKEND_NUM_RNA_QUANTIFICATION_SETS"]
        numExpressionLevels = app.config[
            "SIMULATED_BACKEND_NUM_EXPRESSION_LEVELS_PER_RNA_QUANT_SET"]

        dataRepository = datarepo.SimulatedDataRepository(
            randomSeed=randomSeed, numCalls=numCalls,
            variantDensity=variantDensity, numVariantSets=numVariantSets,
            numReferenceSets=numReferenceSets,
            numReferencesPerReferenceSet=numReferencesPerReferenceSet,
            numReadGroupsPerReadGroupSet=numReadGroupsPerReadGroupSet,
            numAlignments=numAlignmentsPerReadGroup,
            numPhenotypeAssociations=numPhenotypeAssociations,
            numPhenotypeAssociationSets=numPhenotypeAssociationSets,
            numRnaQuantSets=numRnaQuantSets,
            numExpressionLevels=numExpressionLevels)
    elif dataSource.scheme == "empty":
        dataRepository = datarepo.EmptyDataRepository()
    elif dataSource.scheme == "file":
        path = os.path.join(dataSource.netloc, dataSource.path)
        dataRepository = datarepo.SqlDataRepository(path)
        dataRepository.open(datarepo.MODE_READ)
    else:
        raise exceptions.ConfigurationException(
            "Unsupported data source scheme: " + dataSource.scheme)
    theBackend = backend.Backend(dataRepository)
    theBackend.setRequestValidation(app.config["REQUEST_VALIDATION"])
    theBackend.setDefaultPageSize(app.config["DEFAULT_PAGE_SIZE"])
    theBackend.setMaxResponseLength(app.config["MAX_RESPONSE_LENGTH"])
    return theBackend


def configure(configFile=None, baseConfig="ProductionConfig",
              port=8000, extraConfig={}):
    """
    TODO Document this critical function! What does it do? What does
    it assume?
    """
    file_handler = StreamHandler()
    file_handler.setLevel(logging.WARNING)
    app.logger.addHandler(file_handler)
    configStr = 'ga4gh.server.serverconfig:{0}'.format(baseConfig)
    app.config.from_object(configStr)
    if os.environ.get('GA4GH_CONFIGURATION') is not None:
        app.config.from_envvar('GA4GH_CONFIGURATION')
    if configFile is not None:
        app.config.from_pyfile(configFile)
    app.config.update(extraConfig.items())
    # Setup file handle cache max size
    datamodel.fileHandleCache.setMaxCacheSize(
        app.config["FILE_HANDLE_CACHE_MAX_SIZE"])
    # Setup CORS
    try:
        CORS(app, allow_headers='Content-Type')
    except AssertionError:
        pass
    app.serverStatus = ServerStatus()

    app.backend = _configure_backend(app)
    if app.config.get('SECRET_KEY'):
        app.secret_key = app.config['SECRET_KEY']
    elif app.config.get('OIDC_PROVIDER'):
        raise exceptions.ConfigurationException(
            'OIDC configuration requires a secret key')
    if app.config.get('CACHE_DIRECTORY'):
        app.cache_dir = app.config['CACHE_DIRECTORY']
    else:
        app.cache_dir = '/tmp/ga4gh'
    app.cache = FileSystemCache(
        app.cache_dir, threshold=5000, default_timeout=600, mode=384)
    # Peer service initialization
    network.initialize(
        app.config.get('INITIAL_PEERS'),
        app.backend.getDataRepository(),
        app.logger)
    app.oidcClient = None
    app.myPort = port
    if app.config.get('AUTH0_ENABLED'):
        emails = app.config.get('AUTH0_AUTHORIZED_EMAILS', '').split(',')
        [auth.authorize_email(e, app.cache) for e in emails]
    if "OIDC_PROVIDER" in app.config:
        # The oic client. If we're testing, we don't want to verify
        # SSL certificates
        app.oidcClient = oic.oic.Client(
            verify_ssl=('TESTING' not in app.config))
        try:
            app.oidcClient.provider_config(app.config['OIDC_PROVIDER'])
        except requests.exceptions.ConnectionError:
            configResponse = message.ProviderConfigurationResponse(
                issuer=app.config['OIDC_PROVIDER'],
                authorization_endpoint=app.config['OIDC_AUTHZ_ENDPOINT'],
                token_endpoint=app.config['OIDC_TOKEN_ENDPOINT'],
                revocation_endpoint=app.config['OIDC_TOKEN_REV_ENDPOINT'])
            app.oidcClient.handle_provider_config(configResponse,
                                                  app.config['OIDC_PROVIDER'])

        # The redirect URI comes from the configuration.
        # If we are testing, then we allow the automatic creation of a
        # redirect uri if none is configured
        redirectUri = app.config.get('OIDC_REDIRECT_URI')
        if redirectUri is None and app.config.get('TESTING'):
            redirectUri = 'https://{0}:{1}/oauth2callback'.format(
                socket.gethostname(), app.myPort)
        app.oidcClient.redirect_uris = [redirectUri]
        if redirectUri is []:
            raise exceptions.ConfigurationException(
                'OIDC configuration requires a redirect uri')

        # We only support dynamic registration while testing.
        if ('registration_endpoint' in app.oidcClient.provider_info and
           app.config.get('TESTING')):
            app.oidcClient.register(
                app.oidcClient.provider_info["registration_endpoint"],
                redirect_uris=[redirectUri])
        else:
            response = message.RegistrationResponse(
                client_id=app.config['OIDC_CLIENT_ID'],
                client_secret=app.config['OIDC_CLIENT_SECRET'],
                redirect_uris=[redirectUri],
                verify_ssl=False)
            app.oidcClient.store_registration_info(response)

    # Set user access map from file if using a gateway to authenticate
    if app.config.get("TYK_ENABLED"):
        load_access_map()


def chooseReturnMimetype(request):
    mimetype = None
    if hasattr(request, 'accept_mimetypes'):
        mimetype = request.accept_mimetypes.best_match(protocol.MIMETYPES)
    if mimetype is None:
        mimetype = protocol.MIMETYPES[0]
    return mimetype


def getFlaskResponse(responseString, httpStatus=200,
                     mimetype="application/json"):
    """
    Returns a Flask response object for the specified data and HTTP status.
    """
    return flask.Response(responseString, status=httpStatus, mimetype=mimetype)


def federation(endpoint, request, return_mimetype, request_type='POST'):
    """
    Federate the queries by iterating through the peer list and merging the
    result.

    Parameters:
    ===========
    endpoint: method
        Method of a datamodel object that populates the response
    request: string
        Request send along with the query, like: id for GET requests
    return_mimetype: string
        'http/json or application/json'
    request_type: string
        Specify whether the request is a "GET" or a "POST" request

    Returns:
    ========
    responseObject: json string
        Merged responses from the servers. responseObject structure:

    {
    "status": {
        "Successful communications": <number>,
        "Known peers": <number>,
        "Valid response": <true|false>,
        "Queried peers": <number>
        },
    "results": [
            {
            "datasets": [
                    {record1},
                    {record2},
                    ...
                    {recordN},
                ]
            }
        ]
    }

    """
    request_dictionary = flask.request

    if app.config.get("TYK_ENABLED"):
        # Check authorization if using gateway
        if 'Authorization' in request_dictionary.headers:
            authz_token = request_dictionary.headers['Authorization']
            access_map = getAccessMap(authz_token[7:])
        else:
            raise exceptions.NotAuthenticatedException
    else:
        authz_token = ''
        access_map = getAccessMap(authz_token)

    # Self query
    responseObject = {}
    responseObject['results'] = {}
    responseObject['status'] = list()

    try:
        responseObject['results'] = json.loads(
            endpoint(
                request,
                return_mimetype=return_mimetype,
                access_map=access_map
            )
        )

        responseObject['status'].append(200)

    except (exceptions.ObjectWithIdNotFoundException, exceptions.NotFoundException):
        responseObject['status'].append(404)

    except (exceptions.NotAuthorizedException):
        responseObject['status'].append(401)

    # Peer queries
    # Apply federation by default or if it was specifically requested
    if ('Federation' not in request_dictionary.headers or request_dictionary.headers.get('Federation') == 'True'):

        # Iterate through all peers
        for peer in app.serverStatus.getPeers():
            # Generate the same call on the peer
            uri = request_dictionary.url.replace(
                request_dictionary.host_url,
                peer.getUrl(),
            )
            # federation field must be set to False to avoid infinite loop
            header = {
                'Content-Type': return_mimetype,
                'Accept': return_mimetype,
                'Federation': 'False',
                'Authorization': authz_token,
            }

            # Make the call
            try:
                if request_type == 'GET':
                    response = requests.Session().get(
                        uri,
                        headers=header,
                    )
                elif request_type == 'POST':
                    response = requests.Session().post(
                        uri,
                        json=json.loads(request),
                        headers=header,
                    )
                else:
                    # TODO: Raise error
                    pass

                print('  >> peer call: {0} - {1}'.format(uri, response.status_code))

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError):
                responseObject['status'].append(503)

                print('  >> peer call: {0} - {1}'.format(uri, 'SERVER IS DOWN OR DID NOT RESPONSE!'))
            else:
                responseObject['status'].append(response.status_code)
                # If the call was successful append the results
                if response.status_code == 200:
                    try:
                        if request_type == 'GET':
                            responseObject['results'] = response.json()['results']

                        elif request_type == 'POST':
                            peer_response = response.json()['results']

                            if not responseObject['results']:
                                responseObject['results'] = peer_response
                            else:
                                for key in peer_response:
                                    if key == 'nextPageToken':
                                        if 'nextPageToken' not in responseObject['results']:
                                            responseObject['results'][key] = peer_response[key]
                                        continue
                                    for record in peer_response[key]:
                                        responseObject['results'][key].append(record)
                    except ValueError:
                        pass

    # If no result has been found on any of the servers raise an error
    if not responseObject['results'] or not responseObject['results']:
        if request_type == 'GET':
            raise exceptions.ObjectWithIdNotFoundException(request)
        elif request_type == 'POST':
            raise exceptions.ObjectWithIdNotFoundException(json.loads(request))

    # Reformat the status response
    responseObject['status'] = {

        # All the peers plus self
        'Known peers': len(app.serverStatus.getPeers()) + 1,

        # Queried means http status code 200 and 404
        'Queried peers': responseObject['status'].count(200) + responseObject['status'].count(404),

        # Successful means http status code 200
        'Successful communications': responseObject['status'].count(200),

        # Invalid by default
        'Valid response': False
    }

    # Decide on valid response
    if responseObject['status']['Known peers'] == \
            responseObject['status']['Queried peers']:
        if request_type == 'GET':
            if responseObject['status']['Successful communications'] >= 1:
                responseObject['status']['Valid response'] = True
        elif request_type == 'POST':
            if responseObject['status']['Successful communications'] == \
                    responseObject['status']['Queried peers']:
                responseObject['status']['Valid response'] = True

    return json.dumps(responseObject)


# testing method for access roles through token id + access_list.txt
def getAccessMap(token):
    """
    user roles are loaded in to the server from the access_list.txt
    a user-specific access map is generated here based on the id_token and
    the server side access map generated during on server startup

    if running NoAuth config, user has full access to local dataset

    :param token: raw keycloak oidc id_token containing user info
    :return: python dict in the form {"project" : "access tier", ...}
    """

    access_map = {}

    if app.config.get("TYK_ENABLED"):
        parsed_payload = _parseTokenPayload(token)

        username = parsed_payload.get('preferred_username')
        if username:
            access_map = app.access_map.getUserAccessMap().get(username, {})
        else:
            if app.logger:
                app.logger.warn("Token does not contain a valid username")

    else:
        # mock full access to local dataset
        for dataset in app.serverStatus.getDatasets():
            access_map[dataset.getLocalId()] = 4

    return access_map


def _parseTokenPayload(token):

    try:
        token_payload = token.split(".")[1]

        # make sure token is padded properly for b64 decoding
        padding = len(token_payload) % 4
        if padding != 0:
            token_payload += '=' * (4 - padding)
        decoded_payload = base64.b64decode(token_payload)

    except IndexError:
        decoded_payload = '{}'

    return json.loads(decoded_payload)


def handleHttpPost(request, endpoint):
    """
    Handles the specified HTTP POST request, which maps to the specified
    protocol handler endpoint and protocol request class.
    """
    if request.mimetype and request.mimetype not in protocol.MIMETYPES:
        raise exceptions.UnsupportedMediaTypeException()
    return_mimetype = chooseReturnMimetype(request)
    request = request.get_data()
    if request == '' or request is None:
        request = '{}'
    responseStr = federation(
        endpoint,
        request,
        return_mimetype=return_mimetype,
        request_type='POST'
    )
    return getFlaskResponse(responseStr, mimetype=return_mimetype)


def handleList(endpoint, request):
    """
    Handles the specified HTTP GET request, mapping to a list request
    """
    return_mimetype = chooseReturnMimetype(request)
    responseStr = endpoint(request.get_data(), return_mimetype=return_mimetype)
    return getFlaskResponse(responseStr, mimetype=return_mimetype)


def handleHttpGet(id_, endpoint):
    """
    Handles the specified HTTP GET request, which maps to the specified
    protocol handler endpoint and protocol request class
    """
    request = flask.request
    return_mimetype = chooseReturnMimetype(request)
    responseStr = federation(
        endpoint,
        id_,
        return_mimetype=return_mimetype,
        request_type='GET'
    )
    return getFlaskResponse(responseStr, mimetype=return_mimetype)


def handleHttpOptions():
    """
    Handles the specified HTTP OPTIONS request.
    """
    response = getFlaskResponse("")
    response.headers.add("Access-Control-Request-Methods", "GET,POST,OPTIONS")
    return response


@app.errorhandler(Exception)
def handleException(exception):
    """
    Handles an exception that occurs somewhere in the process of handling
    a request.
    """
    serverException = exception
    if not isinstance(exception, exceptions.BaseServerException):
        with app.test_request_context():
            app.log_exception(exception)
        serverException = exceptions.getServerError(exception)
    error = serverException.toProtocolElement()
    # If the exception is being viewed by a web browser, we can render a nicer
    # view.
    if flask.request and 'Accept' in flask.request.headers and \
            flask.request.headers['Accept'].find('text/html') != -1:
        message = "<h1>Error {}</h1><pre>{}</pre>".format(
            serverException.httpStatus,
            protocol.toJson(error))
        if serverException.httpStatus == 401 \
                or serverException.httpStatus == 403:
            message += "Please try <a href=\"" + app.config.get('TYK_LISTEN_PATH') + "login_oidc\">logging in</a>."
        return message
    else:
        # Errors aren't well defined enough to use protobuf, even if requested
        return_mimetype = 'application/json'
        responseStr = protocol.serialize(error, return_mimetype)
        return getFlaskResponse(responseStr, serverException.httpStatus,
                                mimetype=return_mimetype)


def requires_session(f):
    """
    Decorator for browser session routes. Inspects tokens and ensures client sessions + server
    sessions are aligned.
    """

    @functools.wraps(f)
    def decorated(*args, **kargs):
        if app.config.get("TYK_ENABLED"):

            # TODO: CSRF check / checksum
            if not flask.request.cookies.get("session_id"):
                flask.session.clear()
                return flask.redirect(_generate_login_url())

        return f(*args, **kargs)
    return decorated


def startLogin():
    """
    If we are not logged in, this generates the redirect URL to the OIDC
    provider and returns the redirect response
    :return: A redirect response to the OIDC provider
    """
    flask.session["state"] = oic.oauth2.rndstr(SECRET_KEY_LENGTH)
    flask.session["nonce"] = oic.oauth2.rndstr(SECRET_KEY_LENGTH)
    args = {
        "client_id": app.oidcClient.client_id,
        "response_type": "code",
        "scope": ["openid", "profile"],
        "nonce": flask.session["nonce"],
        "redirect_uri": app.oidcClient.redirect_uris[0],
        "state": flask.session["state"]
    }

    result = app.oidcClient.do_authorization_request(
        request_args=args, state=flask.session["state"])
    return flask.redirect(result.url)


@app.before_request
def checkAuthentication():
    """
    The request will have a parameter 'key' if it came from the command line
    client, or have a session key of 'key' if it's the browser.
    If the token is not found, start the login process.

    If there is no oidcClient, we are running naked and we don't check.
    If we're being redirected to the oidcCallback we don't check.

    :returns None if all is ok (and the request handler continues as usual).
    Otherwise if the key was in the session (therefore we're in a browser)
    then startLogin() will redirect to the OIDC provider. If the key was in
    the request arguments, we're using the command line and just raise an
    exception.
    """
    if app.config.get("TYK_ENABLED"):
        if app.access_map.getListUpdated() != os.path.getmtime(app.access_map.getFilePath()):
            if app.logger:
                app.logger.info(
                    "local access_list.txt updated -> reloading backend list"
                )
            load_access_map()
        return
    if app.oidcClient is None:
        return
    if flask.request.endpoint == 'oidcCallback':
        return
    key = flask.session.get('key') or flask.request.args.get('key')
    if key is None or not app.cache.get(key):
        if 'key' in flask.request.args:
            raise exceptions.NotAuthenticatedException()
        else:
            return startLogin()


@app.after_request
def prevent_cache(response):
    """
    Disable response caching for dashboard
    """
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


def handleFlaskGetRequest(id_, flaskRequest, endpoint):
    """
    Handles the specified flask request for one of the GET URLs
    Invokes the specified endpoint to generate a response.
    """
    if flaskRequest.method == "GET":
        return handleHttpGet(id_, endpoint)
    else:
        raise exceptions.MethodNotAllowedException()


def handleFlaskListRequest(id_, flaskRequest, endpoint):
    """
    Handles the specified flask list request for one of the GET URLs.
    Invokes the specified endpoint to generate a response.
    """

    return handleList(endpoint, flaskRequest)


def handleFlaskPostRequest(flaskRequest, endpoint):
    """
    Handles the specified flask request for one of the POST URLS
    Invokes the specified endpoint to generate a response.
    """
    if flaskRequest.method == "POST":
        return handleHttpPost(flaskRequest, endpoint)
    elif flaskRequest.method == "OPTIONS":
        return handleHttpOptions()
    else:
        raise exceptions.MethodNotAllowedException()


class DisplayedRoute(object):
    """
    Registers that a route should be displayed on the html page
    """
    def __init__(
            self, path, postMethod=False, pathDisplay=None):
        self.path = path
        self.methods = None
        if postMethod:
            methodDisplay = 'POST'
            self.methods = SEARCH_ENDPOINT_METHODS
        else:
            methodDisplay = 'GET'
        if pathDisplay is None:
            pathDisplay = path
        app.urls.append((methodDisplay, pathDisplay))

    def __call__(self, func):
        if self.methods is None:
            app.add_url_rule(self.path, func.func_name, func)
        else:
            app.add_url_rule(
                self.path, func.func_name, func, methods=self.methods)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            return result
        return wrapper


@app.route('/')
@requires_session
def index():
    return render_candig_template('spa.html')


@app.route('/serverinfo')
@requires_session
def server_info():
    response = flask.render_template('index.html',
                                     info=app.serverStatus)
    if app.config.get('AUTH0_ENABLED'):
        key = (flask.request.args.get('key'))
        try:
            print(key)
            profile = app.cache.get(key)
        except:
            raise exceptions.NotAuthorizedException()
        if (profile):
            return response
        else:
            exceptions.NotAuthenticatedException()
    else:
        return response


# SEARCH
@DisplayedRoute('/search', postMethod=True)
@requires_auth
def searchQuery():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchQuery)


@DisplayedRoute('/variantsbygenesearch', postMethod=True)
def search_variant_by_gene_name():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchVariantsByGeneName)


@app.route('/login_oidc', methods=LOGIN_ENDPOINT_METHODS)
def login_oidc():
    """
    *** GETs to this endpoint should be set to 'ignore' in Tyk Endpoint settings ***

    All GET requests without an Authorization header should be proxied through
    this endpoint by gateway middleware (except for API token request).

    Identity Provider should treat this route as the callback url for handling oauth2 protocol
    using the form_post method
    """

    base_url = _generate_base_url()

    # GET request: check if already authenticated, check for a return url to save
    if flask.request.method == "GET":

        return_path = flask.request.args.get('returnUri', '', type=str).replace(base_url, '')
        if return_path != "/login_oidc":
            flask.session["return_url"] = base_url + return_path

        # check for valid flask session
        if flask.request.cookies.get("session_id"):
            return flask.redirect(base_url)

        # not logged in, redirect to keycloak (browser) or raise error (API)
        else:
            get_endpoints = [x[1].replace('<id>', '') for x in app.serverStatus.getUrls() if x[0] == 'GET']

            if return_path.startswith(tuple(get_endpoints)):
                return getFlaskResponse(json.dumps({'error': 'Key not authorised'}), 403)

            return flask.redirect(_generate_login_url(base_url + return_path))

    # POST request: successful keycloak authentication or gateway blocks request
    elif flask.request.method == "POST":

        if flask.request.headers.get('KC-Access'):

            # save tokens in server session
            flask.session["access_token"] = flask.request.headers["KC-Access"]
            flask.session["refresh_token"] = flask.request.headers["KC-Refresh"]
            flask.session["id_token"] = flask.request.headers["Authorization"][7:]

            # extract token data used in session
            parsed_payload = _parseTokenPayload(flask.session["id_token"])
            flask.session["username"] = parsed_payload["preferred_username"]
            max_cookie_age = parsed_payload["exp"] - parsed_payload["iat"]

            # redirect user to the page they requested or the default landing page
            redirect_url = flask.session.pop('return_url') if 'return_url' in flask.session else base_url

            # load response
            response = flask.redirect(redirect_url)
            response.set_cookie('session_id', flask.session["id_token"], max_age=max_cookie_age,
                                path=app.config.get('TYK_LISTEN_PATH', '/'), httponly=True)

            return response

        # refresh/back POST
        elif flask.session.get("access_token"):
            return flask.redirect(base_url)

        # keycloak error
        else:
            raise exceptions.NotAuthenticatedException()

    # Invalid method
    else:
        raise exceptions.MethodNotAllowedException()


@app.route('/logout_oidc', methods=["POST"])
def gateway_logout():
    """
    End flask login sessions. Tyk will handle remote keycloak session
    :return: redirect to the keycloak login
    """

    if not flask.request.cookies.get("session_id"):
        raise exceptions.NotAuthenticatedException

    response = flask.Response({})

    # delete browser cookie
    response.set_cookie('session_id', '', expires=0, path=app.config.get('TYK_LISTEN_PATH', '/'), httponly=True)

    # delete server/client sessions
    flask.session.clear()

    return response


def _generate_login_url(return_url=''):
    """
    :param: return_url: URL to return to after successful login
    :return: formatted url for keycloak login
    """
    login_url = '{0}{1}'.format(app.config.get('KC_SERVER'), app.config.get('KC_LOGIN_REDIRECT'))
    if return_url:
        login_url += '&return_url=' + return_url
    return login_url


def _generate_base_url():
    """
    :return: formatted url for TYK proxied dashboard homepage
    """
    return '{0}{1}'.format(app.config.get('TYK_SERVER'), app.config.get('TYK_LISTEN_PATH'))


def _generate_logout_url():
    """
    :return: formatted url for keycloak logout
    """
    return '{0}/auth/realms/{1}/protocol/openid-connect/logout?redirect_uri={2}'.format(
        app.config.get('KC_SERVER'), app.config.get('KC_REALM'), app.config.get('KC_REDIRECT'))


@DisplayedRoute('/token', postMethod=True)
def token():
    """
        :return: an oidc id token to attach to subsequent request headers in the following format:

            Authorization: Bearer <token>

        As of now, the tyk 'pre-auth' js middleware handles the token
        grant API calls and unless there was an authentication error,
        the token should already be in the request header

        Allows token retrieval without having to use a flask session (ie. for REST API calls)
    """

    response = {}
    mimetype = "application/json"

    try:
        token = flask.request.headers["Authorization"][7:]
        parsed_payload = _parseTokenPayload(token)
        token_expires = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(parsed_payload["exp"]))

        response["token"] = token
        response["expires"] = token_expires
        status = 200

    except:
        # get error details from the request
        response["error"] = flask.request.data
        status = 401

    return flask.Response(json.dumps(response), status=status, mimetype=mimetype)


@app.route('/concordance')
def concordance():
    gene = request.args.get('gene', '', type=str)
    if gene == '':
        return jsonify(result = 'Gene symbol is missing')
    concordance, freq, uniq = client.FederatedClient(
        ServerStatus().getPeers()).get_concordance(gene)
    abnormality = NCIT.NCIT().get_genetic_abnormalities(gene)
    disease = NCIT.NCIT().get_diseases(gene)
    return jsonify(result=render_template(
        'concordance.html',
        concordance=concordance,
        freq=freq,
        uniq=uniq,
        abnormality=abnormality,
        disease=disease))


# New configuration added by Kevin Chan
@app.route("/login")
def login():
    if app.config.get('AUTH0_ENABLED'):
        if (flask.request.args.get('code')):
            return auth.render_key(app, key=flask.request.args.get('code'))
        else:
            return auth.render_login(
                app=app,
                scopes=app.config.get('AUTH0_SCOPES'),
                redirect_uri=app.config.get('AUTH0_CALLBACK_URL'),
                domain=app.config.get('AUTH0_HOST'),
                client_id=app.config.get('AUTH0_CLIENT_ID'))
    else:
        raise exceptions.NotFoundException()


@app.route('/callback')
def callback_handling():
    if app.config.get('AUTH0_ENABLED'):
        return auth.callback_maker(
            cache=app.cache,
            domain=app.config.get('AUTH0_HOST'),
            client_id=app.config.get('AUTH0_CLIENT_ID'),
            client_secret=app.config.get('AUTH0_CLIENT_SECRET'),
            redirect_uri=app.config.get('AUTH0_CALLBACK_URL'))()
    else:
        raise exceptions.NotFoundException()


@app.route("/logout")
@requires_auth
@cross_origin(headers=['Content-Type', 'Authorization'])
def logout():
    key = flask.session['auth0_key']
    auth.logout(app.cache)
    url = 'https://{}/v2/logout?access_token={}&?client_id={}'.format(
        app.config.get('AUTH0_HOST'),
        key,
        app.config.get('AUTH0_CLIENT_ID'),
        app.config.get('AUTH0_CALLBACK_URL'))
    return flask.redirect(url)


@app.route('/favicon.ico')
@app.route('/robots.txt')
def robots():
    return flask.send_from_directory(
        app.static_folder, flask.request.path[1:])


@DisplayedRoute('/info')
@requires_auth
def getInfo():
    return handleFlaskGetRequest(
        None, flask.request, app.backend.runGetInfo)


@DisplayedRoute('/references/<id>')
@requires_auth
def getReference(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetReference)


@DisplayedRoute('/referencesets/<id>')
def getReferenceSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetReferenceSet)


@DisplayedRoute('/listreferencebases', postMethod=True)
def listReferenceBases():
    return handleFlaskListRequest(
        id, flask.request, app.backend.runListReferenceBases)


@DisplayedRoute('/callsets/search', postMethod=True)
def searchCallSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchCallSets)


@DisplayedRoute('/readgroupsets/search', postMethod=True)
def searchReadGroupSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchReadGroupSets)


@DisplayedRoute('/reads/search', postMethod=True)
def searchReads():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchReads)


@DisplayedRoute('/referencesets/search', postMethod=True)
def searchReferenceSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchReferenceSets)


@DisplayedRoute('/references/search', postMethod=True)
def searchReferences():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchReferences)


@DisplayedRoute('/variantsets/search', postMethod=True)
def searchVariantSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchVariantSets)


@DisplayedRoute('/variants/search', postMethod=True)
def searchVariants():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchVariants)


@DisplayedRoute('/genotypes/search', postMethod=True)
def searchGenotypes():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchGenotypes)


@DisplayedRoute('/variantannotationsets/search', postMethod=True)
def searchVariantAnnotationSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchVariantAnnotationSets)


@DisplayedRoute('/variantannotations/search', postMethod=True)
def searchVariantAnnotations():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchVariantAnnotations)


@DisplayedRoute('/datasets/search', postMethod=True)
@requires_auth
def searchDatasets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchDatasets)


@DisplayedRoute('/experiments/search', postMethod=True)
@requires_auth
def searchExperiments():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchExperiments)


@DisplayedRoute('/analyses/search', postMethod=True)
@requires_auth
def searchAnalyses():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchAnalyses)


@DisplayedRoute('/featuresets/search', postMethod=True)
@requires_auth
def searchFeatureSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchFeatureSets)


@DisplayedRoute('/features/search', postMethod=True)
@requires_auth
def searchFeatures():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchFeatures)


@DisplayedRoute('/continuoussets/search', postMethod=True)
@requires_auth
def searchContinuousSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchContinuousSets)


@DisplayedRoute('/continuous/search', postMethod=True)
@requires_auth
def searchContinuous():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchContinuous)


@DisplayedRoute('/biosamples/search', postMethod=True)
@requires_auth
def searchBiosamples():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchBiosamples)


@DisplayedRoute('/individuals/search', postMethod=True)
@requires_auth
def searchIndividuals():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchIndividuals)


# METADATA
@DisplayedRoute('/patients/search', postMethod=True)
@requires_auth
def searchPatients():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchPatients)


@DisplayedRoute('/enrollments/search', postMethod=True)
@requires_auth
def searchEnrollments():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchEnrollments)


@DisplayedRoute('/consents/search', postMethod=True)
@requires_auth
def searchConsents():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchConsents)


@DisplayedRoute('/diagnoses/search', postMethod=True)
@requires_auth
def searchDiagnoses():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchDiagnoses)


@DisplayedRoute('/samples/search', postMethod=True)
@requires_auth
def searchSamples():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchSamples)


@DisplayedRoute('/treatments/search', postMethod=True)
@requires_auth
def searchTreatments():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchTreatments)


@DisplayedRoute('/outcomes/search', postMethod=True)
@requires_auth
def searchOutcomes():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchOutcomes)


@DisplayedRoute('/complications/search', postMethod=True)
@requires_auth
def searchComplications():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchComplications)


@DisplayedRoute('/tumourboards/search', postMethod=True)
@requires_auth
def searchTumourboards():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchTumourboards)


@DisplayedRoute('/extractions/search', postMethod=True)
@requires_auth
def searchExtractions():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchExtractions)


@DisplayedRoute('/sequencing/search', postMethod=True)
@requires_auth
def searchSequencing():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchSequencing)


@DisplayedRoute('/alignments/search', postMethod=True)
@requires_auth
def searchAlignments():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchAlignments)


@DisplayedRoute('/variantcalling/search', postMethod=True)
@requires_auth
def searchVariantCalling():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchVariantCalling)


@DisplayedRoute('/fusiondetection/search', postMethod=True)
@requires_auth
def searchFusionDetection():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchFusionDetection)


@DisplayedRoute('/expressionanalysis/search', postMethod=True)
@requires_auth
def searchExpressionAnalysis():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchExpressionAnalysis)


@DisplayedRoute('/peers/list', postMethod=True)
@requires_auth
def listPeers():
    return handleFlaskPostRequest(
        flask.request, app.backend.runListPeers)


@DisplayedRoute('/announce', postMethod=True)
@requires_auth
def announce():
    # We can't use the post handler here because we want detailed request
    # data.
    return app.backend.runAddAnnouncement(flask.request)


@DisplayedRoute(
    '/biosamples/<no(search):id>',
    pathDisplay='/biosamples/<id>')
@requires_auth
def getBiosample(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetBiosample)


@DisplayedRoute(
    '/individuals/<no(search):id>',
    pathDisplay='/individuals/<id>')
@requires_auth
def getIndividual(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetIndividual)


# METADATA
@DisplayedRoute(
    '/patients/<no(search):id>',
    pathDisplay='/patients/<id>')
@requires_auth
def getPatient(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetPatient)


@DisplayedRoute(
    '/enrollments/<no(search):id>',
    pathDisplay='/enrollments/<id>')
@requires_auth
def getEnrollment(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetEnrollment)


@DisplayedRoute(
    '/consents/<no(search):id>',
    pathDisplay='/consents/<id>')
@requires_auth
def getConsent(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetConsent)


@DisplayedRoute(
    '/diagnoses/<no(search):id>',
    pathDisplay='/diagnoses/<id>')
@requires_auth
def getDiagnosis(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetDiagnosis)


@DisplayedRoute(
    '/samples/<no(search):id>',
    pathDisplay='/samples/<id>')
@requires_auth
def getSample(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetSample)


@DisplayedRoute(
    '/treatments/<no(search):id>',
    pathDisplay='/treatments/<id>')
@requires_auth
def getTreatment(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetTreatment)


@DisplayedRoute(
    '/outcomes/<no(search):id>',
    pathDisplay='/outcomes/<id>')
@requires_auth
def getOutcome(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetOutcome)


@DisplayedRoute(
    '/complications/<no(search):id>',
    pathDisplay='/complications/<id>')
@requires_auth
def getComplication(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetComplication)


@DisplayedRoute(
    '/tumourboards/<no(search):id>',
    pathDisplay='/tumourboards/<id>')
@requires_auth
def getTumourboard(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetTumourboard)


@DisplayedRoute(
    '/extractions/<no(search):id>',
    pathDisplay='/extractions/<id>')
@requires_auth
def getExtraction(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetExtraction)


@DisplayedRoute(
    '/sequencing/<no(search):id>',
    pathDisplay='/sequencing/<id>')
@requires_auth
def getSequencing(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetSequencing)


@DisplayedRoute(
    '/alignments/<no(search):id>',
    pathDisplay='/alignments/<id>')
@requires_auth
def getAlignment(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetAlignment)


@DisplayedRoute(
    '/variantcalling/<no(search):id>',
    pathDisplay='/variantcalling/<id>')
@requires_auth
def getVariantCalling(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetVariantCalling)


@DisplayedRoute(
    '/fusiondetections/<no(search):id>',
    pathDisplay='/fusiondetections/<id>')
@requires_auth
def getFusionDetection(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetFusionDetection)


@DisplayedRoute(
    '/expressionanalysis/<no(search):id>',
    pathDisplay='/expressionanalysis/<id>')
@requires_auth
def getExpressionAnalysis(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetExpressionAnalysis)


@DisplayedRoute('/rnaquantificationsets/search', postMethod=True)
@requires_auth
def searchRnaQuantificationSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchRnaQuantificationSets)


@DisplayedRoute('/rnaquantifications/search', postMethod=True)
@requires_auth
def searchRnaQuantifications():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchRnaQuantifications)


@DisplayedRoute('/expressionlevels/search', postMethod=True)
@requires_auth
def searchExpressionLevels():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchExpressionLevels)


@DisplayedRoute(
    '/variantsets/<no(search):id>',
    pathDisplay='/variantsets/<id>')
@requires_auth
def getVariantSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetVariantSet)


@DisplayedRoute(
    '/variants/<no(search):id>',
    pathDisplay='/variants/<id>')
@requires_auth
def getVariant(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetVariant)


@DisplayedRoute(
    '/readgroupsets/<no(search):id>',
    pathDisplay='/readgroupsets/<id>')
@requires_auth
def getReadGroupSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetReadGroupSet)


@DisplayedRoute('/readgroups/<id>')
@requires_auth
def getReadGroup(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetReadGroup)


@DisplayedRoute(
    '/callsets/<no(search):id>',
    pathDisplay='/callsets/<id>')
@requires_auth
def getCallSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetCallSet)


@DisplayedRoute(
    '/featuresets/<no(search):id>',
    pathDisplay='/featuresets/<id>')
@requires_auth
def getFeatureSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetFeatureSet)


@DisplayedRoute(
    '/features/<no(search):id>',
    pathDisplay='/features/<id>')
@requires_auth
def getFeature(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetFeature)


@DisplayedRoute(
    '/continuoussets/<no(search):id>',
    pathDisplay='/continuoussets/<id>')
@requires_auth
def getcontinuousSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetContinuousSet)


@DisplayedRoute(
    '/rnaquantificationsets/<no(search):id>',
    pathDisplay='/rnaquantificationsets/<id>')
@requires_auth
def getRnaQuantificationSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetRnaQuantificationSet)


@DisplayedRoute(
    '/rnaquantifications/<no(search):id>',
    pathDisplay='/rnaquantifications/<id>')
@requires_auth
def getRnaQuantification(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetRnaQuantification)


@DisplayedRoute(
    '/expressionlevels/<no(search):id>',
    pathDisplay='/expressionlevels/<id>')
@requires_auth
def getExpressionLevel(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetExpressionLevel)


@app.route('/oauth2callback', methods=['GET'])
def oidcCallback():
    """
    Once the authorization provider has cleared the user, the browser
    is returned here with a code. This function takes that code and
    checks it with the authorization provider to prove that it is valid,
    and get a bit more information about the user (which we don't use).

    A token is generated and given to the user, and the authorization info
    retrieved above is stored against this token. Later, when a client
    connects with this token, it is assumed to be a valid user.

    :return: A display of the authentication token to use in the client. If
    OIDC is not configured, raises a NotImplementedException.
    """
    if app.oidcClient is None:
        raise exceptions.NotImplementedException()
    response = dict(flask.request.args.iteritems(multi=True))
    aresp = app.oidcClient.parse_response(
        message.AuthorizationResponse,
        info=response,
        sformat='dict')
    sessState = flask.session.get('state')
    respState = aresp['state']
    if (not isinstance(aresp, message.AuthorizationResponse) or
            respState != sessState):
        raise exceptions.NotAuthenticatedException()

    args = {
        "code": aresp['code'],
        "redirect_uri": app.oidcClient.redirect_uris[0],
        "client_id": app.oidcClient.client_id,
        "client_secret": app.oidcClient.client_secret
    }
    atr = app.oidcClient.do_access_token_request(
        scope="openid",
        state=respState,
        request_args=args)

    if not isinstance(atr, message.AccessTokenResponse):
        raise exceptions.NotAuthenticatedException()

    atrDict = atr.to_dict()
    if flask.session.get('nonce') != atrDict['id_token']['nonce']:
        raise exceptions.NotAuthenticatedException()
    key = oic.oauth2.rndstr(SECRET_KEY_LENGTH)
    flask.session['key'] = key
    token_data = aresp["code"], respState, atrDict
    app.cache.set(key, token_data)
    # flask.url_for is broken. It relies on SERVER_NAME for both name
    # and port, and defaults to 'localhost' if not found. Therefore
    # we need to fix the returned url
    indexUrl = flask.url_for('index', _external=True)
    indexParts = list(urlparse.urlparse(indexUrl))
    if ':' not in indexParts[1]:
        indexParts[1] = '{}:{}'.format(socket.gethostname(), app.myPort)
        indexUrl = urlparse.urlunparse(indexParts)
    response = flask.redirect(indexUrl)
    return response


@DisplayedRoute(
    '/datasets/<no(search):id>',
    pathDisplay='/datasets/<id>')
@requires_auth
def getDataset(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetDataset)


@DisplayedRoute(
    '/experiments/<no(search):id>',
    pathDisplay='/experiments/<id>')
@requires_auth
def getExperiment(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetExperiment)


@DisplayedRoute(
    '/analyses/<no(search):id>',
    pathDisplay='/analyses/<id>')
@requires_auth
def getAnalysis(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetAnalysis)


@DisplayedRoute(
    '/variantannotationsets/<no(search):id>',
    pathDisplay='/variantannotationsets/<id>')
@requires_auth
def getVariantAnnotationSet(id):
    return handleFlaskGetRequest(
        id, flask.request, app.backend.runGetVariantAnnotationSet)


@DisplayedRoute('/phenotypes/search', postMethod=True)
@requires_auth
def searchPhenotypes():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchPhenotypes)


@DisplayedRoute('/featurephenotypeassociations/search', postMethod=True)
@requires_auth
def searchGenotypePhenotypes():
    return handleFlaskPostRequest(
        flask.request,
        app.backend.runSearchGenotypePhenotypes)


@DisplayedRoute('/phenotypeassociationsets/search', postMethod=True)
@requires_auth
def searchPhenotypeAssociationSets():
    return handleFlaskPostRequest(
        flask.request, app.backend.runSearchPhenotypeAssociationSets)


# The below methods ensure that JSON is returned for various errors
# instead of the default, html


@app.errorhandler(401)
def unauthorizedHandler(errorString):
    return handleException(exceptions.UnauthorizedException(errorString))


@app.errorhandler(404)
def pathNotFoundHandler(errorString):
    return handleException(exceptions.PathNotFoundException())


@app.errorhandler(405)
def methodNotAllowedHandler(errorString):
    return handleException(exceptions.MethodNotAllowedException())


@app.errorhandler(403)
def notAuthenticatedHandler(errorString):
    return handleException(exceptions.NotAuthenticatedException(errorString))
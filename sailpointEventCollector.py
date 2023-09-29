import json
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
import boto3
import requests
from botocore.exceptions import ClientError
from dateutil.parser import parse
import logging
import os

# Initialize logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    This Lambda function will receive events from the SailPoint IdentityNow V3 Search API and convert them into the
    associated 1.0.0-rc.2 OCSF schema.

    * Only events with matching 'type' included in ocsf_map.json are transformed, all others are ignored.
    * All events are queried from the last event received 'created' time.
    * The time of the last event is stored in the SSM parameter named SailPointOCSFCheckpoint.
    * 10000 max records are returned per run.
    * Transformed events are saved to a temporary location as json for further processing by Glue.
    * Final destination for data is the associated Security Lake custom source S3 bucket in parquet format.

    See https://developer.sailpoint.com/idn/api/v3/search for information on the SailPoint IdentityNow Search API
    See https://schema.ocsf.io/1.0.0-rc.2/ for information about the 1.0.0-rc.2 OCSF schema
    """
    # Get events from the IdentityNow API
    api_events, checkpoint_time = get_events_from_api()
    transformed_events = process_event_data(api_events)
    transformed_events = convert_to_epoch(transformed_events)

    # If there are any transformed_events objects the events are split into class files and saved to S3.
    if len(transformed_events) > 0:
        try:
            lambda_tmp_dir = '/tmp/'

            class_json = {}
            for obj in transformed_events:
                class_name = obj['class_name']
                if class_name not in class_json:
                    class_json[class_name] = []
                class_json[class_name].append(obj)

            for key in class_json:
                json_file_path = f"{lambda_tmp_dir}{key}/ocsfEvents_{checkpoint_time}.json"
                send_transformed_to_json(class_json[key], json_file_path)

            upload_to_s3(lambda_tmp_dir)
            s3location = f"{os.environ['TempFileS3Bucket']}/{os.environ['TempFileS3Bucket']}"
            logger.info('Events transformed and copied to S3 at %s', s3location)
        except Exception as e:
            logger.setLevel(logging.WARNING)
            logger.error('An error has occurred while processing events: %s', str(e))
            raise
    else:
        logger.info('No new events were found, checkpoint time was %s', checkpoint_time)
        sys.exit()

    # Launch the Glue crawler and ETL jobs to transform the data to parquet
    launch_crawler(os.environ['GlueCrawlerName'])
    launch_etl_job(os.environ['GlueETLJob'])

    # Copy the files from the staging location to Lambda local storage
    s3_client = boto3.client('s3')
    source_bucket = os.environ['TempFileS3Bucket']
    source_prefix = "parquet_temp/ext/"
    source_objects = get_s3_objects(s3_client, source_bucket, source_prefix)

    # Copies all parquet files from S3 to local
    for obj in source_objects:
        key = obj['Key']
        local_path = '/tmp/' + key.replace('/', '_')
        s3_client.download_file(source_bucket, key, local_path)

    # Assume role for each Security Lake custom source and copy to custom source
    # Must use the s3a because it supports role assumption
    roles_and_paths = [
        (os.environ['AuthenticationExecutionRole'], "ext/sailpoint-auth"),
        (os.environ['AccountChangeExecutionRole'], "ext/sailpoint-acct-chng"),
        (os.environ['ScheduledJobActivityExecutionRole'], "ext/sailpoint-sched-job")
    ]
    custom_source_bucket = os.environ['SecurityLakeS3Bucket']
    external_id = os.environ['SecurityLakeExternalID']
    for role_name, location in roles_and_paths:
        s3_client = s3_client_assume_role(role_name, external_id)
        for obj in source_objects:
            key = obj['Key']
            if location in key:
                local_path = '/tmp/' + key.replace('/', '_')
                custom_source_key = 'ext/' + key.replace('parquet_temp/ext/', '')
                s3_client.upload_file(local_path, custom_source_bucket, custom_source_key)

    # Delete the temp files after successful parquet file conversion
    delete_s3_data(os.environ['TempFileS3Bucket'], os.environ['TempFilePrefix'])
    delete_s3_data(os.environ['TempFileS3Bucket'], source_prefix)

    # Update the checkpoint time
    parameter_name = 'SailPointOCSFCheckpoint'
    set_parameter_store_checkpoint(parameter_name, checkpoint_time)


def s3_client_assume_role(role_arn, external_id):
    sts_client = boto3.client('sts')
    assumed_role = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName="SecurityLakeSession",
        ExternalId=external_id
    )
    credentials = assumed_role['Credentials']
    s3_client = boto3.client(
        's3',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken']
    )
    return s3_client


def get_s3_objects(s3_client, bucket, prefix):
    results = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if 'Contents' in page:
            results.extend(page['Contents'])
    return results


def delete_s3_data(bucket, prefix):
    s3resource = boto3.resource('s3')
    bucket = s3resource.Bucket(bucket)
    for obj in bucket.objects.filter(Prefix=prefix):
        s3resource.Object(bucket.name, obj.key).delete()


def get_enum_values(class_nm, attribute_nm, caption):
    split_name = attribute_nm.split('.')
    with open('enum_mapping.json', 'r') as f:
        enum_map = json.load(f)
        try:
            if len(split_name) > 1:
                object_nm = split_name[-2]
                attribute_nm = split_name[-1]
                key = f"('{object_nm}', '{attribute_nm}')"
            else:
                key = f"('{class_nm}', '{attribute_nm}')"
            val = enum_map.get(key, {}).get(caption)
            if val is None:
                val = 0
            return val

        except KeyError:
            return 0


def normalize(type, class_nm, attribute_nm, source_str, output_type, concat=''):
    # Load normalized terms
    with open('normalized_terms.json', 'r') as f:
        normalized_terms = json.load(f)
    normalized_values = normalized_terms.get(type)
    norm_value = normalized_values.get(source_str, 'Other')
    if concat:
        norm_value = concat + norm_value
    if output_type == "norm_string":
        return norm_value
    elif output_type == 'integer':
        return get_enum_values(class_nm, attribute_nm, norm_value)


def get_nested_value(event, keys):
    if not isinstance(keys, list):
        keys = [keys]
    for key in keys:
        if isinstance(event, dict):
            event = event.get(key)
        else:
            return None
    return event


def remove_unwanted_keys(json_object, key_name):
    if isinstance(json_object, dict):
        return {i: remove_unwanted_keys(j, key_name) for i, j in json_object.items() if i != key_name and j != key_name}
    elif isinstance(json_object, list):
        return [remove_unwanted_keys(b, key_name) for b in json_object if b != key_name]
    else:
        return json_object


def eval_condition(condition, event):
    if ' not in ' in condition:
        op_index = condition.find(' not in ')
        operator = 'not in'
    else:
        op_index = condition.find(' in ')
        operator = 'in'

    value = condition[:op_index]
    keys = condition[op_index + len(operator) + 2:].split('.')
    field_value = get_nested_value(event, keys)
    if not field_value:
        field_value = "System"
    if operator == 'in':
        return value in field_value
    else:
        return value not in field_value


def transform_event(event, mapping):
    event_type = event['type']
    mapping_type = remove_unwanted_keys(mapping.get(event_type), 'sample_data')

    if mapping_type is not None and mapping_type != []:
        transformed_event = {'data': event}

        for field, value in mapping_type['staticValues'].items():
            set_nested_value(transformed_event, field.split('.'), value)
        class_name = mapping_type['staticValues']['class_name']

        for field, mappings_list in mapping_type['fieldMappings'].items():
            for mapping in mappings_list:
                mapping_value = None
                if 'conditions' in mapping:
                    for condition in mapping['conditions']:
                        if eval_condition(condition['condition'], event):
                            source_field = condition['source_field']
                            mapping_value = get_nested_value(event, source_field.split('.'))
                            break
                else:
                    mapping_value = get_nested_value(event, field.split(','))

                if mapping_value is not None:
                    if isinstance(mapping['target_field'], dict):
                        target_items = mapping['target_field'].items()
                        for new_source_value, target_field in target_items:
                            if 'transform' in mapping and mapping['transform'] == 'normalize':
                                output_type = mapping.get('output_type', 'norm_string') + ''
                                concat_string = mapping.get('concat_string', '') + ''
                                mapping_value = normalize(
                                    event_type,
                                    class_name,
                                    target_field,
                                    new_source_value,
                                    output_type,
                                    concat_string
                                )

                            if target_field.strip():
                                set_nested_value(transformed_event, target_field.split('.'), mapping_value)
                    else:
                        if 'transform' in mapping and mapping['transform'] == 'normalize':
                            mapping_value = normalize(
                                event_type,
                                class_name,
                                mapping['target_field'],
                                mapping_value,
                                mapping.get('output_type', 'norm_string'),
                                mapping.get('concat_string', '')
                            )
                        if mapping['target_field'].strip():
                            set_nested_value(transformed_event, mapping['target_field'].split('.'), mapping_value)
        return transformed_event
    else:
        return None


def get_events_from_api():
    # Get information about IdentityNow from Lambda Environment Vars
    parameter_name = 'SailPointOCSFCheckpoint'
    org_name = os.environ['SailPointOrganizationName']
    identitynow_url = "https://{}.api.identitynow.com".format(org_name)
    client_id = os.environ['SailPointClientID']
    client_secret = get_secret_manager_secret('SailPointClientSecret')  # os.environ['SailPointClientSecret']
    checkpoint_time = get_parameter_store_checkpoint(parameter_name)

    # Build the header.
    headers = build_header(identitynow_url, client_id, client_secret, False)

    x_total_count = 0
    audit_events = []
    partial_set = False

    # Number of Events to return per call to the search API
    try:
        limit = int(os.environ['MaxEventsFromAPI'])
    except ValueError:
        logger.info('Cannot convert %s to an integer. Check value in environment variable MaxEventsFromAPI. '
                    'Defaulting to 1000 Events.', os.environ['MaxEventsFromAPI'])
        limit = 1000
    count = "true"
    offset = 0

    while True:
        if partial_set:
            break

        query_params = build_query_params(count, limit, offset)

        # Search criteria - retrieve all audit events since the checkpoint time, sorted by created date
        search_payload = build_search_payload(checkpoint_time)

        # audit events url
        audit_events_url = identitynow_url + "/v3/search/events"

        # Initiate request
        response = get_search_events_response(
            audit_events_url, headers, query_params, search_payload, False
        )
        print(f'Response Status Code: {response.status_code}')
        # API Gateway saturated / rate limit encountered. Delay and try again.
        # Delay will either be dictated by IdentityNow server response or 5 seconds
        if response.status_code == 429:
            retryDelay = 5
            retryAfter = response.headers["Retry-After"]
            if retryAfter is not None:
                retryDelay = 1000 * int(retryAfter)

            print("429 - Rate Limit Exceeded, retrying in " + str(retryDelay))
            time.sleep(retryDelay)

        elif response.ok:
            # Check response headers to get total number of search results - if this value is 0 there is nothing to
            # parse, if it is less than the limit value then we are caught up to most recent, and can exit the query
            # loop
            x_total_count = int(response.headers["X-Total-Count"])
            if x_total_count > 0:
                if response.json() is not None:
                    try:
                        if x_total_count < limit:
                            # less than limit returned, caught up so exit
                            partial_set = True

                        results = response.json()
                        # Add this set of results to the audit events array
                        audit_events.extend(results)
                        current_last_event = audit_events[-1]
                        checkpoint_time = current_last_event["created"]
                    except KeyError:
                        print("Response does not contain items")
                        break
                    partial_set = True  # Remove this after testing
            else:
                # Set partial_set to True to exit loop (no results)
                partial_set = True
        else:
            logger.setLevel(logging.WARNING)
            logger.error('Failure from API: %s', str(response.status_code))
            raise

    if len(audit_events) > 0:
        last_event = audit_events[-1]
        new_checkpoint_time = last_event["created"]
        logger.info('Total Events received in API response: %s  Last Event Time Captured: %s', str(len(audit_events)),
                    new_checkpoint_time)
        return audit_events, new_checkpoint_time
    else:
        logger.info('No New Events Found')
        return None, None


def process_event_data(source_events):
    # Load source event to OCFS event
    transformed_events = []
    counter = 0
    with open('ocsf_map.json', 'r') as f:
        mappings = json.load(f)
    if source_events is None:
        logger.info('No Events to Process')
        return None
    else:
        for event in source_events:
            try:
                transformed_event = transform_event(event, mappings)
            except Exception as e:
                logger.setLevel(logging.WARNING)
                logger.error('An error has occurred while processing events: %s', str(e))
                raise
            if transformed_event is not None and transformed_event != []:
                counter = counter + 1
                transformed_events.append(transformed_event)
        logger.info('Total OCSF Events Transformed: %s', str(counter))
        return transformed_events


def upload_to_s3(json_file_path, sso=False, profile_name=None):
    try:

        if sso:
            session = boto3.Session(profile_name=profile_name)
            s3_resource = session.resource('s3')
        else:
            s3_resource = boto3.resource('s3')

        aws_bucket_name = os.environ['TempFileS3Bucket']
        aws_folder_path = os.environ['TempFilePrefix']

        for root, directories, files in os.walk(json_file_path):
            rel_path = os.path.relpath(root, json_file_path)
            destination_path = os.path.join(aws_folder_path, rel_path)
            for file in files:
                source_file_path = os.path.join(root, file)
                destination_file_path = os.path.join(destination_path, file)
                s3_resource.Bucket(aws_bucket_name).upload_file(f'{source_file_path}',
                                                                f'{destination_file_path}')
        return True
    except Exception as e:
        logger.setLevel(logging.WARNING)
        logger.error('An error has occurred while processing events: %s', str(e))
        return False


def send_transformed_to_json(transformed_events, destination):
    directory = os.path.dirname(destination)
    if not os.path.exists(directory):
        os.makedirs(directory)

    with open(destination, 'w') as f:
        for i in transformed_events:
            f.write(json.dumps(i) + '\n')


def get_secret_manager_secret(secret_name):
    secretsmanager_client = boto3.client('secretsmanager')
    response = secretsmanager_client.get_secret_value(SecretId=secret_name)
    return response['SecretString']


def get_parameter_store_checkpoint(parameter_name):
    ssm_client = boto3.client('ssm')

    try:
        response = ssm_client.get_parameter(
            Name=parameter_name,
            WithDecryption=False
        )
        return response['Parameter']['Value']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ParameterNotFound':
            logger.setLevel(logging.WARNING)
            logger.error(
                f"Parameter {parameter_name} not found. Please create a Parameter Store parameter named {parameter_name} "
                f"before using this script.")
        else:
            logger.setLevel(logging.WARNING)
            logger.error(f"An error occurred when attempting to access Parameter Store: {e}")
        sys.exit(1)


def set_parameter_store_checkpoint(parameter_name, checkpoint_time, param_type='String'):
    ssm_client = boto3.client('ssm')

    try:
        response = ssm_client.put_parameter(
            Name=parameter_name,
            Value=checkpoint_time,
            Type=param_type,
            Overwrite=True
        )
        return response
    except ClientError as e:
        if e.response['Error']['Code'] == 'ParameterNotFound':
            logger.setLevel(logging.WARNING)
            logger.error(
                f'Parameter {parameter_name} not found. Please create a Parameter Store parameter named {parameter_name}'
                f' before using this script.')
        else:
            logger.setLevel(logging.WARNING)
            logger.error(f'An error occurred when attempting to access Parameter Store: {e}')
        sys.exit(1)


def use_current(now, old):
    ret = False
    try:
        a = datetime.strptime(now, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        a = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")

    try:
        b = datetime.strptime(old, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        b = datetime.strptime(old, "%Y-%m-%dT%H:%M:%SZ")

    diff = a - b
    delta_days = diff.days

    if int(delta_days) > 0:
        ret = True

    return ret


def is_https(identitynow_url):
    scheme = urlparse(identitynow_url).scheme
    if scheme.lower() == "https":
        print("INFO IdentityNow URL is HTTPS.")
        return True
    else:
        print("ERROR IdentityNow URL is not HTTPS.")
        return False


def get_token(identitynow_url, client_id, client_secret, use_proxy):
    token_url = identitynow_url + "/oauth/token"

    # Check if url is https.
    if not is_https(token_url):
        return False

    # JWT RETRIEVAL
    # The following request is responsible for retrieving a valid JWT token from the IdentityNow tenant
    token_params = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    response = requests.request("POST", token_url, data=token_params)
    print(f'TOKEN:{response}')
    return response


def build_oauth2_header(identitynow_url, client_id, client_secret, use_proxy):
    token_response = get_token(identitynow_url, client_id, client_secret, use_proxy)

    if token_response is not None and 200 <= token_response.status_code < 300:
        access_token = token_response.json()["access_token"]
        return {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
        }
    elif token_response.json().get("error_description") is not None:
        raise ConnectionError(token_response.json().get("error_description"))
    else:
        raise ConnectionError("Unable to fetch headers from IdentityNow!")


def build_header(identitynow_url, client_id, client_secret, use_proxy):
    if identitynow_url and client_id and client_secret:
        # If identityNow url, client_id & client_secret exists then construct oauth.
        return build_oauth2_header(identitynow_url, client_id, client_secret, use_proxy)
    else:
        # This should not happen.
        print("Error No credentials were provided.")
        return None


def get_checkpoint_time(content):
    content = [x.strip() for x in content]

    # need to operate on a 5 minute delay
    new_checkpoint_time = (
                                  datetime.utcnow() - timedelta(minutes=60)
                          ).isoformat() + "Z"

    # Set checkpoint time to either the current timestamp, or what was saved in the checkpoint file
    if len(content) == 1:
        checkpoint_time = content[0]
        if use_current(new_checkpoint_time, checkpoint_time):
            checkpoint_time = new_checkpoint_time
            return checkpoint_time
        print(f'Checkpoint Time:{checkpoint_time}')
        return checkpoint_time
    else:
        checkpoint_time = new_checkpoint_time
        print(f'Checkpoint Time:{checkpoint_time}')
        return checkpoint_time


def get_search_events_response(
        audit_events_url, headers, query_params, search_payload, use_proxy
):
    # Check if search_events_url is https.
    if not is_https(audit_events_url):
        print("INFO Search Events URL is not HTTPS.")
        return False
    logger.info('Trying to execute the search %s', str(audit_events_url))
    try:
        response = requests.post(audit_events_url, params=query_params, data=json.dumps(search_payload),
                                 headers=headers)
        logger.info('Got a response %s', str(response))
    except Exception as e:
        logger.setLevel(logging.WARNING)
        logger.error('An error has occurred while accessing the API: %s', str(e))
        raise
    logger.info('Got a response I think %s', audit_events_url)
    return response


def build_search_payload(checkpoint_time):
    # Search API results are slightly delayed, allow for 5 minutes though in reality
    # this time will be much shorter. Cap query at checkpoint time to 5 minutes ago

    if checkpoint_time is None or checkpoint_time == "":
        checkpoint_time

    search_delay_time = (datetime.utcnow() - timedelta(minutes=60)
                         ).isoformat() + "Z"

    query_checkpoint_time = (
        checkpoint_time.replace("-", "\\-").replace(".", "\\.").replace(":", "\\:")
    )
    query_search_delay_time = (
        search_delay_time.replace("-", "\\-").replace(".", "\\.").replace(":", "\\:")
    )

    print(
        f"checkpoint_time {query_checkpoint_time} search_delay_time {query_search_delay_time}"
    )

    # Query only for events that are transformable via ocsf_map.json
    with open('ocsf_map.json') as f:
        ocsf_map = json.load(f)
    types = list(ocsf_map.keys())
    query_search_event_types = " OR ".join(f"type:{key}" for key in types)

    # Search criteria - retrieve all audit events since the checkpoint time, sorted by created date
    search_payload = {
        "queryType": "SAILPOINT",
        "query": {
            "query": f"created:>{query_checkpoint_time} AND created:<{query_search_delay_time} AND ({query_search_event_types})"
        },
        "queryResultFilter": {},
        "sort": ["created"],
        "searchAfter": [],
    }
    print(f'Search Payload:{search_payload}')
    return search_payload


def build_query_params(count, limit, offset):
    max_limit = 10000
    if not offset or offset < 0:
        offset = 0

    if not limit or limit > max_limit:
        limit = max_limit

    query_params = {"count": count, "offset": offset, "limit": limit}

    return query_params


def set_nested_value(event, keys, value):
    if not isinstance(keys, list):
        keys = [keys]
    for key in keys[:-1]:
        event = event.setdefault(key, {})
    event[keys[-1]] = value


def convert_to_epoch(json_data):
    if isinstance(json_data, list):
        for i in json_data:
            convert_to_epoch(i)
    elif isinstance(json_data, dict):
        for key in json_data:
            if isinstance(json_data[key], dict):
                if key != 'data':
                    convert_to_epoch(json_data[key])
            else:
                try:
                    dt = parse(json_data[key])
                    epoch = int(time.mktime(dt.timetuple()))
                    json_data[key] = epoch
                except:
                    pass
    return json_data


def validate_input(definition):
    pass


def launch_crawler(crawler_name):
    glue_client = boto3.client('glue')
    glue_client.start_crawler(Name=crawler_name)
    while True:
        crawler = glue_client.get_crawler(Name=crawler_name)
        if crawler['Crawler']['State'] == 'READY':
            break
        logger.info('Glue Crawler %s running... waiting for READY', crawler_name)
        time.sleep(15)

    logger.info('Glue Crawler %s State: %s', crawler_name, crawler['Crawler']['State'])


def launch_etl_job(job_name):
    glue_client = boto3.client('glue')
    sts_client = boto3.client('sts')
    job_run = glue_client.start_job_run(
        JobName=job_name,
        Arguments={
            '--database_name': os.environ['GlueDatabaseName'],
            '--bucket_name': os.environ['TempFileS3Bucket'],
            '--region': sts_client.meta.region_name,
            '--accountId': sts_client.get_caller_identity()["Account"]
        }
    )
    while True:
        status = glue_client.get_job_run(JobName=job_name, RunId=job_run['JobRunId'])
        if status['JobRun']['JobRunState'] in ('FAILED', 'STOPPED', 'SUCCEEDED'):
            break
        logger.info('Glue ETL Job %s running... waiting for SUCCEEDED', job_name)
        time.sleep(15)
    logger.info('Glue ETL Job %s State: %s', job_name, status['JobRun']['JobRunState'])

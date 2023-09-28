import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from pyspark.sql.functions import col
from pyspark.sql.functions import date_format
from pyspark.sql.functions import lit
from awsglue.dynamicframe import DynamicFrame
import logging

# Initialize Contexts
sc = SparkContext()
glueContext = GlueContext(sc)

args = getResolvedOptions(sys.argv, ['database_name', 'bucket_name', 'region', 'accountId'])
database_name = args['database_name']
bucket_name = args['bucket_name']
region = args['region']
accountId = args['accountId']

# Initialize logger
logger = logging.getLogger()
logger.setLevel(logging.WARNING)

# List of table names and source locations
tables_and_locations = [
    ("account_change", "parquet_temp/ext/sp-account-change"),
    ("authentication", "parquet_temp/ext/sp-authentication"),
    ("scheduled_job_activity", "parquet_temp/ext/sp-scheduled-job-act")
]

for table_name, source_location in tables_and_locations:
    try:
        # Read data from Glue catalog
        dynamic_frame = glueContext.create_dynamic_frame.from_catalog(database=database_name, table_name=table_name)

        # Convert to data frame for better manipulation
        data_frame = dynamic_frame.toDF()

        if data_frame.count() > 0:
            # Extract eventDay from data.created and add it as a new column
            data_frame = data_frame.withColumn("eventDay", date_format(col("data.created"), "yyyyMMdd"))

            # Add 'region' and 'accountId' as new columns
            data_frame = data_frame.withColumn("region", lit(region))
            data_frame = data_frame.withColumn("accountId", lit(accountId))

            # Convert back to dynamic frame
            dynamic_frame = DynamicFrame.fromDF(data_frame, glueContext, "dynamic_frame")
            print(f"Number of {table_name} records: {str(dynamic_frame.count())}")

            # Write data back to S3 in Parquet format, partitioned by region, accountId, and eventDay
            glueContext.write_dynamic_frame.from_options(
                frame=dynamic_frame,
                connection_type="s3",
                connection_options={
                    "path": f"s3://{bucket_name}/{source_location}",
                    "partitionKeys": ["region", "accountId", "eventDay"]
                },
                format="parquet"
            )

    except Exception as e:
        logger.error(f"Error processing table {table_name} with source location {source_location}: {str(e)}")
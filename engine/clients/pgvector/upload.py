import time
from typing import List, Optional
import psycopg2
from engine.base_client import BaseUploader
from engine.clients.pgvector.config import *


class PGVectorUploader(BaseUploader):
    conn = None
    upload_params = {}
    distance: str = None
    engine_type: str = None  # rust or c
    vector_count: int = None

    @classmethod
    def init_client(cls, host, distance, vector_count, connection_params, upload_params,
                    extra_columns_name: list, extra_columns_type: list):
        database, host, port, user, password = process_connection_params(connection_params, host)
        cls.conn = psycopg2.connect(database=database, user=user, password=password, host=host, port=port)
        cls.host = host
        cls.upload_params = upload_params
        cls.engine_type = upload_params.get("engine_type", "c")
        cls.distance = DISTANCE_MAPPING_CREATE[distance] if cls.engine_type == "c" else DISTANCE_MAPPING_CREATE_RUST[
            distance]
        cls.vector_count = vector_count

    @classmethod
    def upload_batch(cls, ids: List[int], vectors: List[list], metadata: List[Optional[dict]]):
        if len(ids) != len(vectors):
            raise RuntimeError("PGVector batch upload unhealthy")
        # Getting the names of structured data columns based on the first meta information.
        col_name_tuple = ('id', 'vector')
        col_type_tuple = ('%s', "replace(replace(%s::text, '{', '['), '}', ']')::vecf16")
        if metadata[0] is not None:
            for col_name in list(metadata[0].keys()):
                col_name_tuple += (col_name,)
                col_type_tuple += ('%s',)

        insert_data = []
        for i in range(0, len(ids)):
            temp_tuple = (ids[i], vectors[i])
            if metadata[i] is not None:
                for col_name in list(metadata[i].keys()):
                    value = metadata[i][col_name]
                    # Determining if the data is a dictionary type of latitude and longitude.
                    if isinstance(value, dict) and ('lon' and 'lat') in list(value.keys()):
                        raise RuntimeError("Postgres doesn't support geo datasets")
                    else:
                        temp_tuple += (value,)
            insert_data.append(temp_tuple)

        insert_command = f"INSERT INTO {PGVECTOR_INDEX} ({', '.join(col_name_tuple)}) VALUES ({', '.join(col_type_tuple)})"
        while True:
            try:
                with cls.conn.cursor() as cur:
                    cur.executemany(insert_command, insert_data)
                    cls.conn.commit()
                break
            except Exception as e:
                cls.conn.rollback()
                print(f"PGVector upload exception {e}")
                time.sleep(3)

    @classmethod
    def post_upload(cls, distance):
        index_options_c = ""
        index_options_rust = ""
        for key in cls.upload_params.get("index_params", {}).keys():
            index_options_c += ("{}={}" if index_options_c == "" else ", {}={}").format(
                key, cls.upload_params.get('index_params', {})[key])
            index_options_rust += ("{}={}" if index_options_rust == "" else "\n{}={}").format(
                key, cls.upload_params.get('index_params', {})[key])
        create_index_command = f"CREATE INDEX ON {PGVECTOR_INDEX} USING hnsw (vector {cls.distance}) WITH ({index_options_c});"
        if cls.engine_type == "rust":
            create_index_command = f"""
CREATE INDEX ON {PGVECTOR_INDEX} USING vectors (vector {cls.distance}) WITH (options=$$
optimizing.optimizing_threads=8
segment.max_sealed_segment_size=5000000
[indexing.hnsw]
$$);
"""

        # create index (blocking)
        with cls.conn.cursor() as cur:
            print(create_index_command)
            cur.execute(create_index_command)
            cls.conn.commit()
        # wait index finished
        with cls.conn.cursor() as cur:
            indexing = [True for _ in range(10)]
            while indexing.count(True) != 0:
                time.sleep(1)
                cur.execute("SELECT idx_indexing FROM pg_vector_index_info;")
                indexing.pop(0)
                indexing.append(cur.fetchone()[0])
            cls.conn.commit()

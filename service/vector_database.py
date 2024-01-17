import weaviate

from abc import ABC, abstractmethod
from typing import Any, List, Type
from decouple import config
from litellm import embedding
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest
from pinecone import Pinecone, ServerlessSpec

from models.vector_database import VectorDatabase


class VectorService(ABC):
    def __init__(self, index_name: str, dimension: int, credentials: dict):
        self.index_name = index_name
        self.dimension = dimension
        self.credentials = credentials

    @abstractmethod
    async def upsert():
        pass

    @abstractmethod
    async def query():
        pass

    @abstractmethod
    async def convert_to_rerank_format():
        pass

    async def _generate_vectors(sefl, input: str):
        vectors = []
        embedding_object = embedding(
            model="huggingface/intfloat/multilingual-e5-large",
            input=input,
            api_key=config("HUGGINGFACE_API_KEY"),
        )
        for vector in embedding_object.data:
            if vector["object"] == "embedding":
                vectors.append(vector["embedding"])
        return vectors

    async def rerank(self, query: str, documents: list, top_n: int = 4):
        from cohere import Client

        api_key = config("COHERE_API_KEY")
        if not api_key:
            raise ValueError("API key for Cohere is not present.")
        cohere_client = Client(api_key=api_key)
        docs = [doc["content"] for doc in documents]
        re_ranked = cohere_client.rerank(
            model="rerank-multilingual-v2.0",
            query=query,
            documents=docs,
            top_n=top_n,
        ).results
        results = []
        for r in re_ranked:
            doc = documents[r.index]
            results.append(doc)
        return results


class PineconeVectorService(VectorService):
    def __init__(self, index_name: str, dimension: int, credentials: dict):
        super().__init__(
            index_name=index_name, dimension=dimension, credentials=credentials
        )
        pinecone = Pinecone(api_key=credentials["api_key"])
        if index_name not in [index.name for index in pinecone.list_indexes()]:
            pinecone.create_index(
                name=self.index_name,
                dimension=1024,
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region="us-west-2"),
            )
        self.index = pinecone.Index(name=self.index_name)

    async def convert_to_rerank_format(self, chunks: List):
        docs = [
            {
                "content": chunk.get("metadata")["content"],
                "page_label": chunk.get("metadata")["page_label"],
                "file_url": chunk.get("metadata")["file_url"],
            }
            for chunk in chunks
        ]
        return docs

    async def upsert(self, embeddings: List[tuple[str, list, dict[str, Any]]]):
        self.index.upsert(vectors=embeddings)

    async def query(self, input: str, top_k: 4, include_metadata: bool = True):
        vectors = []
        embedding_object = embedding(
            model="huggingface/intfloat/multilingual-e5-large",
            input=input,
            api_key=config("HUGGINGFACE_API_KEY"),
        )
        for vector in embedding_object.data:
            if vector["object"] == "embedding":
                vectors.append(vector["embedding"])
        results = self.index.query(
            vector=vectors,
            top_k=top_k,
            include_metadata=include_metadata,
        )
        return results["matches"]


class QdrantService(VectorService):
    def __init__(self, index_name: str, dimension: int, credentials: dict):
        super().__init__(
            index_name=index_name, dimension=dimension, credentials=credentials
        )
        self.client = QdrantClient(
            url=credentials["host"], api_key=credentials["api_key"], https=True
        )
        collections = self.client.get_collections()
        if index_name not in [c.name for c in collections.collections]:
            self.client.create_collection(
                collection_name=self.index_name,
                vectors_config={
                    "content": rest.VectorParams(
                        size=1024, distance=rest.Distance.COSINE
                    )
                },
                optimizers_config=rest.OptimizersConfigDiff(
                    indexing_threshold=0,
                ),
            )

    async def convert_to_rerank_format(self, chunks: List[rest.PointStruct]):
        docs = [
            {
                "content": chunk.payload.get("content"),
                "page_label": chunk.payload.get("page_label"),
                "file_url": chunk.payload.get("file_url"),
            }
            for chunk in chunks
        ]
        return docs

    async def upsert(self, embeddings: List[tuple[str, list, dict[str, Any]]]) -> None:
        points = []

        for _embedding in embeddings:
            points.append(
                rest.PointStruct(
                    id=_embedding[0],
                    vector={"content": _embedding[1]},
                    payload={**_embedding[2]},
                )
            )

        self.client.upsert(collection_name=self.index_name, wait=True, points=points)

    async def query(self, input: str, top_k: int) -> List:
        vectors = []
        embedding_object = embedding(
            model="huggingface/intfloat/multilingual-e5-large",
            input=input,
            api_key=config("HUGGINGFACE_API_KEY"),
        )
        for vector in embedding_object.data:
            if vector["object"] == "embedding":
                vectors.append(vector["embedding"])
        search_result = self.client.search(
            collection_name=self.index_name,
            query_vector=("content", vectors),
            limit=top_k,
            # query_filter=rest.Filter(
            #    must=[
            #        rest.FieldCondition(
            #            key="datasource_id",
            #            match=rest.MatchValue(value=datasource_id),
            #        ),
            #    ]
            # ),
            with_payload=True,
        )
        return search_result


class WeaviateService(VectorService):
    def __init__(self, index_name: str, dimension: int, credentials: dict):
        super().__init__(
            index_name=index_name, dimension=dimension, credentials=credentials
        )
        self.client = weaviate.Client(
            url=credentials["host"],
            auth_client_secret=weaviate.AuthApiKey(api_key=credentials["api_key"]),
        )
        schema = {
            "class": self.index_name,
            "properties": [
                {
                    "name": "text",
                    "dataType": ["text"],
                }
            ],
        }
        if not self.client.schema.exists(self.index_name):
            self.client.schema.create_class(schema)

    async def convert_to_rerank_format(self, chunks: List) -> List:
        docs = [
            {
                "content": chunk.get("text"),
                "page_label": chunk.get("page_label"),
                "file_url": chunk.get("file_url"),
            }
            for chunk in chunks
        ]
        return docs

    async def upsert(self, embeddings: List[tuple[str, list, dict[str, Any]]]) -> None:
        with self.client.batch as batch:
            for _embedding in embeddings:
                params = {
                    "uuid": _embedding[0],
                    "data_object": {"text": _embedding[2]["content"], **_embedding[2]},
                    "class_name": self.index_name,
                    "vector": _embedding[1],
                }
                batch.add_data_object(**params)
            batch.flush()

    async def query(self, input: str, top_k: int = 4) -> List:
        vectors = await self._generate_vectors(input=input)
        vector = {"vector": vectors}
        result = (
            self.client.query.get(
                self.index_name.capitalize(),
                ["text", "file_url", "page_label"],
            )
            .with_near_vector(vector)
            .with_limit(top_k)
            .do()
        )
        return result["data"]["Get"][self.index_name.capitalize()]


def get_vector_service(
    index_name: str, credentials: VectorDatabase, dimension: int = 1024
) -> Type[VectorService]:
    services = {
        "pinecone": PineconeVectorService,
        "qdrant": QdrantService,
        "weaviate": WeaviateService,
        # Add other providers here
        # e.g "weaviate": WeaviateVectorService,
    }
    service = services.get(credentials.type.value)
    if service is None:
        raise ValueError(f"Unsupported provider: {credentials.type.value}")
    return service(
        index_name=index_name,
        dimension=dimension,
        credentials=dict(credentials.config),
    )

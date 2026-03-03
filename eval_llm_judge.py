import os
import json
from typing import List, Dict, Any

from openai import OpenAI

def evaluate_generation_llm_judge(query: str, retrieved_recipes: List[Dict[str, Any]], generated_response: str) -> Dict[str, float]:
    """
    Evaluates Answer Relevance and Faithfulness using an LLM as a judge.
    
    Args:
        query: The user's original question.
        retrieved_recipes: The context (recipes) given to the generator.
        generated_response: The final answer produced by your RAG pipeline.
        
    Returns:
        Dict: Scores for 'relevance' and 'faithfulness' out of 1.0
    """
    client = OpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    
    # We use a structured JSON output to easily parse the scores
    system_prompt = """You are an impartial judge evaluating a RAG (Retrieval-Augmented Generation) system.
    You will be given the User Query, the Context (retrieved recipes), and the Generated Answer.
    
    Please evaluate the Generated Answer on two metrics:
    1. Relevance (0.0 to 1.0): How well does the answer address the user's specific query? 
       (e.g., if the user asked for a quick meal, did it recommend a 4-hour recipe? If yes, low score).
    2. Faithfulness (0.0 to 1.0): Did the generated answer hallucinate information not present in the context? 
       (e.g., adding an ingredient not in the recipe).
    
    Return your evaluation as a JSON object with exactly these keys:
    {
        "relevance_score": 0.0 - 1.0,
        "relevance_reason": "string",
        "faithfulness_score": 0.0 - 1.0,
        "faithfulness_reason": "string"
    }
    """
    
    # Format the context so the LLM can read the recipes
    context_str = "\n".join([f"Recipe {i+1}: {json.dumps(r)}" for i, r in enumerate(retrieved_recipes)])
    
    user_prompt = f"""
    --- USER QUERY ---
    {query}
    
    --- CONTEXT (Retrieved Recipes) ---
    {context_str}
    
    --- GENERATED ANSWER ---
    {generated_response}
    """
    
    eval_model = os.environ.get("EVAL_MODEL", "gpt-4o-mini") # gpt-4o or gpt-4 is better for a judge
    
    try:
        response = client.chat.completions.create(
            model=eval_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0, # Use greedy decoding for evaluation consistency
        )
        
        result_json = json.loads(response.choices[0].message.content)
        return {
            "relevance": result_json.get("relevance_score", 0.0),
            "relevance_reason": result_json.get("relevance_reason", ""),
            "faithfulness": result_json.get("faithfulness_score", 0.0),
            "faithfulness_reason": result_json.get("faithfulness_reason", "")
        }
        
    except Exception as e:
        print(f"Error during LLM evaluation: {e}")
        return {"relevance": 0.0, "faithfulness": 0.0}


# --- Mock Example Usage ---
if __name__ == "__main__":
    test_query = "Find me a quick high protein chicken recipe."
    
    mock_retrieved_results = [
        {
            "title": "Quick Grilled Chicken Breast", 
            "ingredients": ["1 chicken breast", "1 tbsp olive oil", "salt", "pepper"],
            "instructions": "Coat chicken in oil and spices. Grill for 6 minutes per side."
        }
    ]
    
    mock_good_response = "You can make a Quick Grilled Chicken Breast! Just coat 1 chicken breast in olive oil, salt, and pepper, and grill for 6 mins per side."
    mock_bad_response = "You can make a Quick Grilled Chicken Breast! Just add lemon juice and garlic powder and bake for 45 minutes." # Hallucination and irrelevant time
    
    print("Evaluating GOOD response:")
    good_scores = evaluate_generation_llm_judge(test_query, mock_retrieved_results, mock_good_response)
    print(json.dumps(good_scores, indent=2))
    
    print("\nEvaluating BAD response:")
    bad_scores = evaluate_generation_llm_judge(test_query, mock_retrieved_results, mock_bad_response)
    print(json.dumps(bad_scores, indent=2))

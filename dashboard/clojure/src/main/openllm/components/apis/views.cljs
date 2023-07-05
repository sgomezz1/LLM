(ns openllm.components.apis.views
  (:require [openllm.components.apis.events :as events]
            [openllm.components.apis.subs :as subs]
            [openllm.components.apis.data :as data]
            [openllm.components.model-selection.views :as model-selection-view]
            [openllm.components.common.views :as ui]
            [re-highlight.core :as hl]
            [re-frame.core :as rf]
            ["highlight.js/lib/languages/json" :as json]))

(hl/register-language "json" json)

;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;
;;                                                                             ;;
;;                             IMPORTANT NOTE                                  ;;
;;                                                                             ;;
;;    Idially, to add new endpoint you need to add it to endpoints data and    ;;
;;    you never have to modify the code below this note!                       ;;
;;    Please refer to the openllm.components.apis.data namespace to find       ;;
;;    said data.                                                                ;;
;;                                                                             ;;
;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;;

(defn single-endpoint
  "A single endpoint in the list of endpoints."
  [endpoint-data]
  (let [selected-api (rf/subscribe [::subs/selected-api])]
    [(fn []
       [:div {:class "mt-2"}
        [:button {:class (str "rounded-l-md w-full py-2 px-2 text-left text-sm font-medium text-gray-700"
                              (if (= @selected-api (:id endpoint-data))
                                " bg-gray-700 text-white font-bold"
                                " bg-gray-200 hover:bg-gray-100 hover:text-gray-900  border-t border-l border-b border-gray-300"))
                  :on-click #(rf/dispatch [::events/set-selected-api (:id endpoint-data)])}
         (str (:name endpoint-data))]])]))

(defn endpoints-list
  "The list on the left containing all the endpoints available."
  []
  (into [:div]
        (map single-endpoint data/endpoints-data)))

(defn request-input-field
  "The input field for the data to send to the backend."
  [selected-api value]
  [:textarea {:class "pt-3 mt-1 font-mono appearance-none w-full h-64 block border border-gray-300 rounded-md shadow-sm placeholder-gray-400 focus:outline-none focus:ring-gray-500 focus:border-gray-500 sm:text-sm"
              :value value
              :on-change #(rf/dispatch [::events/set-input-value selected-api (.. % -target -value)])}])

(defn input-field-controls
  "Control buttons for the input field, where the user enters his/her
   request body."
  [selected-api value]
  [:div {:class "grid grid-cols-2"}
   "maybe"
   [:div {:class "mt-3 flex justify-end"}
    [:button {:class "px-4 py-2 mr-2 text-white bg-gray-600 rounded-md hover:bg-gray-700 focus:outline-none"
              :type "button"
              :on-click #(rf/dispatch [::events/set-input-value selected-api ""])} "Clear"]
    [:button {:class "px-4 py-2 text-white bg-gray-600 rounded-md hover:bg-gray-700 focus:outline-none"
              :type "button"
              :on-click #(rf/dispatch [::events/on-send-button-click selected-api value])} "Send"]]])

(defn result-area
  "The latest response retrieved from the backend will be displayed in
   this component."
  []
  (let [last-message (rf/subscribe [::subs/response-message])]
    (fn []
      [:div
       [ui/headline "Response" 0]
       [hl/highlight {:language "json"} @last-message]])))

(defn endpoint-request-response
  "The request and response area of the selected endpoint."
  []
  (let [selected-api (rf/subscribe [::subs/selected-api])
        input-value (rf/subscribe [::subs/input-value])]
    (fn []
      [:div {:class "mt-2 ml-2"}
       [ui/headline "Request" 0]
       [request-input-field @selected-api @input-value]
       [input-field-controls @selected-api @input-value]
       [result-area]])))

(defn apis-tab-contents
  "The contents of the APIs tab."
  []
  [:div {:class "mt-6 px-4 h-[calc(100%-14.5rem)]"}
   [model-selection-view/model-selection]
   [:div {:class "mt-6 grid-container grid grid-cols-4 h-full"} ;; 4 cols: div 1 will span 1 col, the other one 3
    [:div {:class "col-span-1"}
     [ui/headline "Endpoint" 6]
     [:hr {:class "border-gray-200 mt-1"}]
     [endpoints-list]]
    [:div {:class "col-span-3 border-l border-gray-200 h-full overflow-y-scroll no-scrollbar"}
     [ui/headline "Data" 6]
     [:hr {:class "border-gray-200 mt-1 w-fill"}]
     [endpoint-request-response]]]])
